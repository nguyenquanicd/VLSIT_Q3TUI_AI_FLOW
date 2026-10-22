"""Task input of the VLSIT testbench step (adapted from <step>/md/tb_generator.md; Vietnamese as in the original)."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections

# the stage system prompts are the named sections of md/skill.md (this file only assembles the per-task input)
TEXT = skill_sections(Path(__file__).parent / "md" / "skill.md")

PLAN_SYSTEM = TEXT["PLAN_SYSTEM"]

WRITE_SYSTEM = TEXT["WRITE_SYSTEM"]


def plan_prompt(*, reqs: list[tuple[str, str, str]], params: dict[str, str], spec_files: list[str], top: str | None,
                previous: list[dict] | None, changes: list[str], answers: dict[str, str], settled: str, feedback: list[str],
                uncovered: list[str]) -> str:
    parts = ["# Task: test plan" + (" (cập nhật)" if previous else "")]
    parts.append(f"\nSpec: {', '.join(spec_files) or 'spec/'}" + (f"\nModule top (DUT): `{top}`" if top else ""))
    parts.append("\n## Requirement\n" + "\n".join(f"- {rid} [{cat}]: {text}" for rid, cat, text in reqs))
    parts.append("\n## Tham số đã chốt (final_config.json)\n" + ("\n".join(f"- {k} = {v}" for k, v in params.items()) or "(không có)"))
    if previous:
        parts.append("\n## Test plan hiện tại (giữ nguyên id; chỉ thay đổi phần bị ảnh hưởng)\n"
                     + "\n".join(f"- {c['tc_id']} {c['name']} → {', '.join(c['req_ids'])}: {c.get('description', '')}" for c in previous))
        if changes:
            parts.append("\n## Đầu vào đã thay đổi\n" + "\n".join(f"- {c}" for c in changes))
    if uncovered:
        parts.append("\n## Chưa có TC cover (thêm TC, hoặc giải thích trong questions vì sao không kiểm tra được bằng mô phỏng)\n"
                     + "\n".join(f"- {r}" for r in uncovered))
    if answers:
        parts.append("\n## Câu trả lời của người dùng\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()))
    if settled:
        parts.append(settled)
    if feedback:
        parts.append("\n## Yêu cầu thay đổi\n" + "\n".join(f"- {f}" for f in feedback))
    parts.append("\nTrả về danh sách đầy đủ `test_cases` (cả TC không đổi) và `questions`.")
    return "\n".join(parts)


def top_prompt(*, path: str, top: str | None, params: dict[str, str], spec_files: list[str], answers: dict[str, str],
               settled: str, feedback: list[str], problems: str, existing: bool, cases: list[dict]) -> str:
    parts = [f"# Task: viết testbench top → `{path}`", f"\nDUT: module `{top or '(xem spec)'}`; interface (port, clock, reset, giao thức) lấy từ spec: "
             f"{', '.join(spec_files) or 'spec/'}.",
             "\n## Tham số (instantiate DUT với các giá trị này)\n" + ("\n".join(f"- {k} = {v}" for k, v in params.items()) or "(không có)"),
             "\n## Test case sẽ chạy (mỗi cái là một task `tc_<nnn>_<name>()` trong file riêng, do người khác viết; dùng tên tín hiệu/task của bạn)\n"
             + "\n".join(f"- tc_{int(c['tc_id'].split('-')[-1]):03d}_{c['name']}: {c.get('description', '')}" for c in cases),
             "\nChuẩn bị trong tb_top các tín hiệu và task trợ giúp cần cho các TC đó (ví dụ: reset_dut(), wait_cycles(n), bus read/write task) "
             "và GHI CHÚ ngắn đầu mỗi task trợ giúp (tên, tham số, hành vi) để người viết TC dùng."]
    if answers:
        parts.append("\nCâu trả lời của người dùng:\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()))
    if settled:
        parts.append(settled)
    if feedback:
        parts.append("\nYêu cầu thay đổi:\n" + "\n".join(f"- {f}" for f in feedback))
    if existing:
        parts.append(f"\n`{path}` đã tồn tại: đọc rồi chỉ sửa phần cần sửa.")
    if problems:
        parts.append("\nVấn đề Q3TUI còn thấy (sửa hết):\n" + problems)
    return "\n".join(parts)


def group_prompt(*, cases: list[dict], paths: dict[str, str], reqs: dict[str, str], params: dict[str, str], spec_files: list[str],
                 top_path: str, answers: dict[str, str], settled: str, feedback: list[str], problems: str,
                 existing: set[str]) -> str:
    parts = ["# Task: viết các test case sau (mỗi TC một file, chỉ ghi các file này)"]
    for c in cases:
        parts.append(f"\n### {c['tc_id']} {c['name']} → `{paths[c['tc_id']]}`" + (" (đã tồn tại: đọc rồi sửa)" if c["tc_id"] in existing else "")
                     + f"\n{c.get('description', '')}\nREQ: " + "; ".join(f"{r}: {reqs.get(r, '')}" for r in c["req_ids"]))
    parts.append("\n## Tham số đã chốt\n" + ("\n".join(f"- {k} = {v}" for k, v in params.items()) or "(không có)"))
    parts.append(f"\nĐọc `{top_path}` để biết tín hiệu và task trợ giúp có sẵn trong tb_top (task TC được include vào bên trong module đó). "
                 f"Spec: {', '.join(spec_files) or 'spec/'}.")
    if answers:
        parts.append("\nCâu trả lời của người dùng:\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()))
    if settled:
        parts.append(settled)
    if feedback:
        parts.append("\nYêu cầu thay đổi:\n" + "\n".join(f"- {f}" for f in feedback))
    if problems:
        parts.append("\nVấn đề Q3TUI còn thấy (kết quả công cụ là sự thật; sửa nguyên nhân):\n" + problems)
    return "\n".join(parts)


def fix_prompt(*, label: str, problems: str, files: dict[str, str]) -> str:
    """A fresh small session: the problems and the current files."""
    body = "\n\n".join(f"`{p}`:\n```systemverilog\n{t}\n```" for p, t in files.items())
    return (f"# Task: sửa testbench ({label})\nTrình biên dịch/Q3TUI báo các lỗi sau (sự thật, sửa nguyên nhân):\n{problems}\n\n"
            f"Nội dung hiện tại:\n{body}\n\nSửa bằng Edit/Write rồi trả về tóm tắt.")
