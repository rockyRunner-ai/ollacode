"""Ollama performance benchmark tool.

Measures token generation speed, prefill speed, TTFT, and memory usage
across progressive requests to detect performance degradation.

Supports reproducible workloads via fixed prompts, seed, and temperature=0.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import psutil
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


# ─── Default workload prompts (deterministic sequence) ────────
DEFAULT_PROMPTS = [
    "Write a Python function called `merge_sorted_lists` that merges two sorted lists into one sorted list. Include type hints.",
    "Add comprehensive error handling to the function above. Handle cases like None inputs and non-list types.",
    "Write 5 unit tests for `merge_sorted_lists` using pytest. Cover edge cases like empty lists and single-element lists.",
    "Refactor the function to support merging N sorted lists using a heap-based approach. Keep backward compatibility.",
    "Add detailed docstrings (Google style) and inline comments explaining the algorithm complexity.",
    "Write a Python class `SortedCollection` that wraps a sorted list with methods: insert, remove, find, and range_query.",
    "Add `__iter__`, `__len__`, `__contains__`, and `__repr__` magic methods to `SortedCollection`.",
    "Write a binary search implementation that `SortedCollection.find` uses internally. Support custom key functions.",
    "Add serialization support to `SortedCollection`: `to_json()`, `from_json()`, and `to_csv()` methods.",
    "Write a performance comparison between `SortedCollection` and Python's built-in `bisect` module for 10K elements.",
    "Create a `LRUCache` class with O(1) get/put using a dict + doubly linked list. Include max_size parameter.",
    "Add TTL (time-to-live) support to `LRUCache`. Expired entries should be lazily evicted on access.",
    "Write a thread-safe version of `LRUCache` using `threading.Lock`. Ensure no deadlocks.",
    "Add metrics tracking to `LRUCache`: hit_rate, miss_rate, eviction_count, avg_access_time.",
    "Write a decorator `@cached(max_size=128, ttl=60)` that uses `LRUCache` to memoize function results.",
    "Create a simple HTTP rate limiter class using the token bucket algorithm. Support burst and sustained rates.",
    "Add a sliding window rate limiter as an alternative strategy. Compare the two approaches.",
    "Write an async version of the rate limiter that works with `asyncio`. Use `asyncio.Lock` for thread safety.",
    "Create a comprehensive benchmark comparing token bucket vs sliding window: throughput, fairness, and memory usage.",
    "Write a summary report of all the code written above. List all classes, their methods, and complexity analysis.",
]

# Sustained mode: single prompt repeated
SUSTAINED_PROMPT = "Write a Python function that validates an email address using regex. Include type hints, error handling, docstring, and 3 example test cases."

KOREAN_SYSTEM_PROMPT = """\
당신은 **ollacode**, 전문 코딩 어시스턴트입니다. /no_think

## 역할
- 사용자의 코딩 질문에 정확하고 실용적인 답변을 제공합니다.
- 코드 리뷰, 디버깅, 리팩토링, 새 코드 작성을 도와줍니다.
- 설명은 간결하되 핵심을 놓치지 않습니다.
- 파일을 수정할 때는 반드시 먼저 read_file로 현재 내용을 확인한 후 edit_file로 수정합니다.

## 도구 사용법
파일 조작이나 명령 실행이 필요할 때, 아래 JSON 형식의 도구 호출 블록을 사용하세요.
반드시 ```tool 코드블록 안에 JSON을 넣어주세요.
한 응답에 여러 도구를 호출할 수 있습니다.

### 사용 가능한 도구

1. **파일 읽기** — 파일 내용을 줄 번호와 함께 표시
```tool
{"tool": "read_file", "path": "파일경로"}
```

2. **파일 생성** — 새 파일을 생성할 때만 사용
```tool
{"tool": "write_file", "path": "파일경로", "content": "파일내용"}
```

3. **파일 편집** ⭐ — 기존 파일의 일부분만 수정 (권장!)
```tool
{"tool": "edit_file", "path": "파일경로", "search": "찾을 텍스트 (정확히 일치해야 함)", "replace": "바꿀 텍스트"}
```
주의: search는 반드시 파일 내 정확히 존재하는 문자열이어야 합니다. 줄바꿈도 포함하세요.

4. **디렉토리 목록**
```tool
{"tool": "list_directory", "path": "디렉토리경로"}
```

5. **파일 이름 검색**
```tool
{"tool": "search_files", "pattern": "*.py", "path": "검색경로"}
```

6. **파일 내용 검색** (grep)
```tool
{"tool": "grep_search", "query": "검색어", "path": "검색경로"}
```

7. **명령 실행**
```tool
{"tool": "run_command", "command": "실행할 명령어"}
```

## 작업 흐름 (중요!)
1. 파일 수정 시: `read_file` → 내용 확인 → `edit_file`로 부분 수정
2. 새 파일: `write_file`로 생성
3. 코드 작성 후: 가능하면 `run_command`로 검증 (lint, test 등)
4. 오류 발생 시: 오류 메시지를 분석하고 자동으로 수정 재시도

## 응답 가이드라인
- 코드는 반드시 적절한 언어의 코드블록으로 감싸세요.
- 한국어와 영어를 자연스럽게 혼용합니다.
- 불필요하게 긴 설명은 피하고, 코드로 보여주세요.
- 도구를 사용한 후에는 결과를 사용자에게 간단히 요약해주세요.
"""

ENGLISH_SYSTEM_PROMPT = """\
You are **ollacode**, an expert coding assistant. /no_think

## Role
- Provide accurate, practical answers to coding questions.
- Help with code review, debugging, refactoring, and writing new code.
- Be concise but thorough. Show code, not long explanations.
- Always read a file with read_file before modifying it with edit_file.
- Respond in the same language the user uses.

## Tools
Call tools using ```tool blocks with JSON. Multiple tool calls per response are allowed.

Available tools:
- `read_file(path)` — Read file with line numbers
- `write_file(path, content)` — Create a new file
- `edit_file(path, search, replace)` — Partial edit via search/replace (preferred for modifications)
- `list_directory(path)` — List directory contents
- `search_files(pattern, path)` — Find files by glob pattern
- `grep_search(query, path)` — Search text inside files
- `run_command(command)` — Execute a shell command

Format:
```tool
{"tool": "read_file", "path": "some/file.py"}
```

## Workflow
1. Modify files: `read_file` → review → `edit_file` (partial edit)
2. New files: `write_file`
3. After writing code: verify with `run_command` (lint, test, etc.)
4. On error: analyze and auto-retry fix
"""


@dataclass
class RoundResult:
    """Result metrics for a single benchmark round."""
    round_num: int
    prompt_tokens: int = 0        # prompt_eval_count
    output_tokens: int = 0        # eval_count
    gen_speed: float = 0.0        # tokens/sec (generation)
    prefill_speed: float = 0.0    # tokens/sec (prompt processing)
    ttft_ms: float = 0.0          # time to first token (ms)
    total_ms: float = 0.0         # total duration (ms)
    memory_mb: float = 0.0        # Ollama process RSS (MB)
    error: str = ""


@dataclass
class BenchmarkReport:
    """Full benchmark report."""
    model: str
    mode: str
    system_prompt_label: str = ""
    rounds: int = 0
    seed: int = 42
    temperature: float = 0.0
    timestamp: str = ""
    results: list[RoundResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> BenchmarkReport:
        results = [RoundResult(**r) for r in d.pop("results", [])]
        return cls(**d, results=results)


class OllamaBenchmark:
    """Ollama performance benchmark runner."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "qwen3-coder:30b",
        seed: int = 42,
        temperature: float = 0.0,
    ) -> None:
        self.host = host
        self.model = model
        self.seed = seed
        self.temperature = temperature
        self._client = httpx.Client(
            base_url=host,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def _get_ollama_memory_mb(self) -> float:
        """Get RSS memory of all ollama-related processes in MB."""
        total_rss = 0.0
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if "ollama" in name or "ollama" in cmdline:
                    mem = proc.memory_info()
                    total_rss += mem.rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total_rss / (1024 * 1024)

    def _send_request(
        self, messages: list[dict[str, str]]
    ) -> tuple[dict, float]:
        """Send a non-streaming chat request and return metrics.

        Returns (response_data, wall_clock_ms).
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "seed": self.seed,
            },
        }

        start = time.perf_counter()
        resp = self._client.post("/api/chat", json=payload)
        wall_ms = (time.perf_counter() - start) * 1000
        resp.raise_for_status()
        return resp.json(), wall_ms

    def _extract_metrics(
        self, data: dict, wall_ms: float, memory_mb: float, round_num: int
    ) -> RoundResult:
        """Extract metrics from Ollama API response."""
        eval_count = data.get("eval_count", 0)
        eval_duration = data.get("eval_duration", 1)  # nanoseconds
        prompt_eval_count = data.get("prompt_eval_count", 0)
        prompt_eval_duration = data.get("prompt_eval_duration", 1)
        total_duration = data.get("total_duration", 0)

        gen_speed = eval_count / eval_duration * 1e9 if eval_duration > 0 else 0
        prefill_speed = (
            prompt_eval_count / prompt_eval_duration * 1e9
            if prompt_eval_duration > 0
            else 0
        )
        ttft_ms = prompt_eval_duration / 1e6  # ns -> ms

        return RoundResult(
            round_num=round_num,
            prompt_tokens=prompt_eval_count,
            output_tokens=eval_count,
            gen_speed=round(gen_speed, 2),
            prefill_speed=round(prefill_speed, 2),
            ttft_ms=round(ttft_ms, 1),
            total_ms=round(total_duration / 1e6, 1),
            memory_mb=round(memory_mb, 1),
        )

    def run_context_growth(
        self,
        prompts: list[str] | None = None,
        rounds: int = 20,
        system_prompt: str = ENGLISH_SYSTEM_PROMPT,
        system_prompt_label: str = "english",
    ) -> BenchmarkReport:
        """Run context growth benchmark — history accumulates."""
        prompts = (prompts or DEFAULT_PROMPTS)[:rounds]
        if len(prompts) < rounds:
            # Cycle prompts if not enough
            while len(prompts) < rounds:
                prompts.append(prompts[len(prompts) % len(DEFAULT_PROMPTS)])

        report = BenchmarkReport(
            model=self.model,
            mode="context-growth",
            system_prompt_label=system_prompt_label,
            rounds=rounds,
            seed=self.seed,
            temperature=self.temperature,
            timestamp=datetime.now().isoformat(),
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        console.print(
            Panel(
                f"[bold]Model:[/bold] {self.model}\n"
                f"[bold]Mode:[/bold] context-growth\n"
                f"[bold]Rounds:[/bold] {rounds}\n"
                f"[bold]Seed:[/bold] {self.seed} | Temp: {self.temperature}\n"
                f"[bold]Prompt:[/bold] {system_prompt_label}",
                title="[bold magenta]🏋️ Ollama Benchmark[/bold magenta]",
                border_style="magenta",
            )
        )

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Round", justify="center", width=6)
        table.add_column("In Tok", justify="right", width=8)
        table.add_column("Out Tok", justify="right", width=8)
        table.add_column("Gen t/s", justify="right", width=9)
        table.add_column("Prefill t/s", justify="right", width=11)
        table.add_column("TTFT(ms)", justify="right", width=9)
        table.add_column("Total(ms)", justify="right", width=10)
        table.add_column("Mem(MB)", justify="right", width=9)

        for i in range(rounds):
            prompt = prompts[i]
            messages.append({"role": "user", "content": prompt})

            console.print(f"  [dim]Round {i+1}/{rounds}...[/dim]", end=" ")

            try:
                mem_before = self._get_ollama_memory_mb()
                data, wall_ms = self._send_request(messages)
                mem_after = self._get_ollama_memory_mb()
                memory = max(mem_before, mem_after)

                # Add assistant response to history for next round
                assistant_content = data.get("message", {}).get("content", "")
                messages.append({"role": "assistant", "content": assistant_content})

                result = self._extract_metrics(data, wall_ms, memory, i + 1)
                report.results.append(result)

                speed_color = "green" if result.gen_speed > 50 else "yellow" if result.gen_speed > 20 else "red"
                table.add_row(
                    str(i + 1),
                    str(result.prompt_tokens),
                    str(result.output_tokens),
                    f"[{speed_color}]{result.gen_speed:.1f}[/{speed_color}]",
                    f"{result.prefill_speed:.1f}",
                    f"{result.ttft_ms:.0f}",
                    f"{result.total_ms:.0f}",
                    f"{result.memory_mb:.0f}",
                )
                console.print(f"[green]✓[/green] {result.gen_speed:.1f} t/s")

            except Exception as e:
                error_result = RoundResult(round_num=i + 1, error=str(e))
                report.results.append(error_result)
                console.print(f"[red]✗ {e}[/red]")
                # Remove failed user message from history
                messages.pop()

        console.print()
        console.print(table)
        self._print_summary(report)
        return report

    def run_sustained(
        self,
        prompt: str | None = None,
        rounds: int = 20,
        system_prompt: str = ENGLISH_SYSTEM_PROMPT,
        system_prompt_label: str = "english",
    ) -> BenchmarkReport:
        """Run sustained load benchmark — independent requests."""
        prompt = prompt or SUSTAINED_PROMPT

        report = BenchmarkReport(
            model=self.model,
            mode="sustained",
            system_prompt_label=system_prompt_label,
            rounds=rounds,
            seed=self.seed,
            temperature=self.temperature,
            timestamp=datetime.now().isoformat(),
        )

        console.print(
            Panel(
                f"[bold]Model:[/bold] {self.model}\n"
                f"[bold]Mode:[/bold] sustained\n"
                f"[bold]Rounds:[/bold] {rounds}\n"
                f"[bold]Seed:[/bold] {self.seed} | Temp: {self.temperature}\n"
                f"[bold]Prompt:[/bold] {system_prompt_label}",
                title="[bold magenta]🏋️ Ollama Benchmark[/bold magenta]",
                border_style="magenta",
            )
        )

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Round", justify="center", width=6)
        table.add_column("In Tok", justify="right", width=8)
        table.add_column("Out Tok", justify="right", width=8)
        table.add_column("Gen t/s", justify="right", width=9)
        table.add_column("Prefill t/s", justify="right", width=11)
        table.add_column("TTFT(ms)", justify="right", width=9)
        table.add_column("Total(ms)", justify="right", width=10)
        table.add_column("Mem(MB)", justify="right", width=9)

        for i in range(rounds):
            # Fresh messages each round
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]

            console.print(f"  [dim]Round {i+1}/{rounds}...[/dim]", end=" ")

            try:
                mem_before = self._get_ollama_memory_mb()
                data, wall_ms = self._send_request(messages)
                mem_after = self._get_ollama_memory_mb()
                memory = max(mem_before, mem_after)

                result = self._extract_metrics(data, wall_ms, memory, i + 1)
                report.results.append(result)

                speed_color = "green" if result.gen_speed > 50 else "yellow" if result.gen_speed > 20 else "red"
                table.add_row(
                    str(i + 1),
                    str(result.prompt_tokens),
                    str(result.output_tokens),
                    f"[{speed_color}]{result.gen_speed:.1f}[/{speed_color}]",
                    f"{result.prefill_speed:.1f}",
                    f"{result.ttft_ms:.0f}",
                    f"{result.total_ms:.0f}",
                    f"{result.memory_mb:.0f}",
                )
                console.print(f"[green]✓[/green] {result.gen_speed:.1f} t/s")

            except Exception as e:
                error_result = RoundResult(round_num=i + 1, error=str(e))
                report.results.append(error_result)
                console.print(f"[red]✗ {e}[/red]")

        console.print()
        console.print(table)
        self._print_summary(report)
        return report

    def _print_summary(self, report: BenchmarkReport) -> None:
        """Print summary statistics."""
        valid = [r for r in report.results if not r.error]
        if not valid:
            console.print("[red]No successful rounds.[/red]")
            return

        first = valid[0]
        last = valid[-1]

        speeds = [r.gen_speed for r in valid]
        avg_speed = sum(speeds) / len(speeds)
        min_speed = min(speeds)
        max_speed = max(speeds)

        ttfts = [r.ttft_ms for r in valid]
        avg_ttft = sum(ttfts) / len(ttfts)

        mems = [r.memory_mb for r in valid if r.memory_mb > 0]
        mem_start = mems[0] if mems else 0
        mem_end = mems[-1] if mems else 0

        # Sparkline
        sparkline = _make_sparkline(speeds)

        speed_change = ((last.gen_speed - first.gen_speed) / first.gen_speed * 100) if first.gen_speed > 0 else 0
        speed_icon = "▼" if speed_change < 0 else "▲"
        speed_color = "red" if speed_change < -10 else "green" if speed_change > -5 else "yellow"

        mem_change = mem_end - mem_start

        console.print(
            Panel(
                f"[bold]Gen speed:[/bold] {first.gen_speed:.1f} → {last.gen_speed:.1f} t/s "
                f"[{speed_color}]({speed_icon}{abs(speed_change):.1f}%)[/{speed_color}]\n"
                f"[bold]  Avg/Min/Max:[/bold] {avg_speed:.1f} / {min_speed:.1f} / {max_speed:.1f} t/s\n"
                f"[bold]TTFT:[/bold] {first.ttft_ms:.0f} → {last.ttft_ms:.0f} ms (avg: {avg_ttft:.0f} ms)\n"
                f"[bold]Memory:[/bold] {mem_start:.0f} → {mem_end:.0f} MB ({'+' if mem_change >= 0 else ''}{mem_change:.0f} MB)\n"
                f"[bold]Sparkline:[/bold] {sparkline}",
                title=f"[bold cyan]📊 Summary — {report.system_prompt_label}[/bold cyan]",
                border_style="cyan",
            )
        )

    @staticmethod
    def save_report(report: BenchmarkReport, path: str) -> None:
        """Save benchmark report to JSON."""
        Path(path).write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        console.print(f"[success]💾 Results saved to {path}[/success]")

    @staticmethod
    def load_report(path: str) -> BenchmarkReport:
        """Load benchmark report from JSON."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return BenchmarkReport.from_dict(data)

    @staticmethod
    def compare_reports(report_a: BenchmarkReport, report_b: BenchmarkReport) -> None:
        """Compare two benchmark reports side by side."""
        valid_a = [r for r in report_a.results if not r.error]
        valid_b = [r for r in report_b.results if not r.error]

        if not valid_a or not valid_b:
            console.print("[red]Cannot compare — one or both reports have no data.[/red]")
            return

        def _avg(items: list[RoundResult], key: str) -> float:
            vals = [getattr(r, key) for r in items]
            return sum(vals) / len(vals) if vals else 0

        def _fmt_change(before: float, after: float, lower_is_better: bool = False) -> str:
            if before == 0:
                return "N/A"
            change = (after - before) / before * 100
            if lower_is_better:
                icon = "✅" if change < 0 else "⚠️"
            else:
                icon = "✅" if change > 0 else "⚠️"
            sign = "+" if change > 0 else ""
            return f"{sign}{change:.1f}% {icon}"

        avg_speed_a = _avg(valid_a, "gen_speed")
        avg_speed_b = _avg(valid_b, "gen_speed")
        avg_ttft_a = _avg(valid_a, "ttft_ms")
        avg_ttft_b = _avg(valid_b, "ttft_ms")
        avg_prefill_a = _avg(valid_a, "prefill_speed")
        avg_prefill_b = _avg(valid_b, "prefill_speed")

        mems_a = [r.memory_mb for r in valid_a if r.memory_mb > 0]
        mems_b = [r.memory_mb for r in valid_b if r.memory_mb > 0]
        peak_mem_a = max(mems_a) if mems_a else 0
        peak_mem_b = max(mems_b) if mems_b else 0

        label_a = report_a.system_prompt_label or "A"
        label_b = report_b.system_prompt_label or "B"

        table = Table(
            title=f"📊 Benchmark Comparison: {label_a} vs {label_b}",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Metric", style="bold", width=18)
        table.add_column(label_a, justify="right", width=12)
        table.add_column(label_b, justify="right", width=12)
        table.add_column("Change", justify="right", width=14)

        table.add_row(
            "Avg Gen (t/s)",
            f"{avg_speed_a:.1f}",
            f"{avg_speed_b:.1f}",
            _fmt_change(avg_speed_a, avg_speed_b, lower_is_better=False),
        )
        table.add_row(
            "Avg TTFT (ms)",
            f"{avg_ttft_a:.0f}",
            f"{avg_ttft_b:.0f}",
            _fmt_change(avg_ttft_a, avg_ttft_b, lower_is_better=True),
        )
        table.add_row(
            "Avg Prefill (t/s)",
            f"{avg_prefill_a:.1f}",
            f"{avg_prefill_b:.1f}",
            _fmt_change(avg_prefill_a, avg_prefill_b, lower_is_better=False),
        )
        table.add_row(
            "Peak Memory (MB)",
            f"{peak_mem_a:.0f}",
            f"{peak_mem_b:.0f}",
            _fmt_change(peak_mem_a, peak_mem_b, lower_is_better=True),
        )

        # Sparklines
        sparkline_a = _make_sparkline([r.gen_speed for r in valid_a])
        sparkline_b = _make_sparkline([r.gen_speed for r in valid_b])

        console.print()
        console.print(table)
        console.print(f"\n  [bold]{label_a}:[/bold] {sparkline_a}")
        console.print(f"  [bold]{label_b}:[/bold] {sparkline_b}")
        console.print()


def _make_sparkline(values: list[float]) -> str:
    """Create a sparkline string from values."""
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    return "".join(
        blocks[min(len(blocks) - 1, int((v - mn) / rng * (len(blocks) - 1)))]
        for v in values
    )


def run_benchmark_cli(args) -> None:
    """Entry point for benchmark CLI subcommand."""
    from .config import Config
    config = Config.load()

    model = args.model or config.ollama_model
    host = config.ollama_host

    # Compare mode
    if args.compare:
        if len(args.compare) != 2:
            console.print("[red]--compare requires exactly 2 JSON files.[/red]")
            return
        report_a = OllamaBenchmark.load_report(args.compare[0])
        report_b = OllamaBenchmark.load_report(args.compare[1])
        OllamaBenchmark.compare_reports(report_a, report_b)
        return

    # Load custom workload
    custom_prompts = None
    if args.workload:
        try:
            wl = json.loads(Path(args.workload).read_text(encoding="utf-8"))
            custom_prompts = wl.get("prompts", DEFAULT_PROMPTS)
            console.print(f"[dim]Loaded workload: {args.workload} ({len(custom_prompts)} prompts)[/dim]")
        except Exception as e:
            console.print(f"[red]Failed to load workload: {e}[/red]")
            return

    # System prompt selection
    if args.system_prompt == "korean":
        system_prompt = KOREAN_SYSTEM_PROMPT
        prompt_label = "korean"
    elif args.system_prompt == "english":
        system_prompt = ENGLISH_SYSTEM_PROMPT
        prompt_label = "english"
    elif args.system_prompt and Path(args.system_prompt).exists():
        system_prompt = Path(args.system_prompt).read_text(encoding="utf-8")
        prompt_label = Path(args.system_prompt).stem
    else:
        system_prompt = ENGLISH_SYSTEM_PROMPT
        prompt_label = "english"

    bench = OllamaBenchmark(
        host=host,
        model=model,
        seed=args.seed,
        temperature=args.temperature,
    )

    try:
        if args.bench_mode == "sustained":
            report = bench.run_sustained(
                rounds=args.rounds,
                system_prompt=system_prompt,
                system_prompt_label=prompt_label,
            )
        else:
            report = bench.run_context_growth(
                prompts=custom_prompts,
                rounds=args.rounds,
                system_prompt=system_prompt,
                system_prompt_label=prompt_label,
            )

        if args.output:
            bench.save_report(report, args.output)

    finally:
        bench.close()
