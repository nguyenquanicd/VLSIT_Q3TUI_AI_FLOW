"""Task input of the `sva` step — adapted from <step>/md/sva_generator.md (Vietnamese, as the original)."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections

# the stage system prompts are the named sections of md/skill.md (this file only assembles the per-task input)
TEXT = skill_sections(Path(__file__).parent / "md" / "skill.md")

SYSTEM = TEXT["SYSTEM"]

FORMAT = TEXT["FORMAT"]


def module_prompt(*, module: str, rtl: str, reqs: str, hints: str, config: str, tcs: str, previous: str, feedback: list[str],
                  settled: str, problems: list[str] | None = None) -> str:
    parts = [f"Viết SVA cho module `{module}` (file `{module}_sva.sv`).", FORMAT,
             f"## REQ-ID giao cho module này\n{reqs}", f"## Gợi ý SVA từ spec parser (sva_hint)\n{hints or '(không có)'}",
             f"## Cấu hình đã khoá (final_config)\n{config or '(mặc định)'}",
             f"## Test case đã chọn cho các REQ này (testbench kiểm chúng; assertion bổ sung, không lặp)\n{tcs or '(chưa có)'}",
             f"## RTL của `{module}` (nguồn thật — tên tín hiệu phải khớp)\n```systemverilog\n{rtl}\n```"]
    if settled:
        parts.append(settled)
    if previous:
        parts.append("## File SVA hiện tại (cập nhật nó; giữ nguyên những assertion người dùng đã xác nhận trừ khi thay đổi bắt buộc)\n"
                     f"```systemverilog\n{previous}\n```")
    if feedback:
        parts.append("## Yêu cầu thay đổi (áp dụng)\n" + "\n".join(f"- {f}" for f in feedback))
    if problems:
        parts.append("## Vấn đề Q3TUI kiểm tra được — sửa hết\n" + "\n".join(f"- {p}" for p in problems))
    parts.append("Trả về `content` (cả file), `summary` (REQ nào chưa có assertion và vì sao), `questions`.")
    return "\n\n".join(parts)
