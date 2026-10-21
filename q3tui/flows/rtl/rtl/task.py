"""Task input of the VLSIT RTL step (adapted from <step>/md/rtl_generator.md; Vietnamese as in the original)."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections

# the stage system prompts are the named sections of md/skill.md (this file only assembles the per-task input)
TEXT = skill_sections(Path(__file__).parent / "md" / "skill.md")

import json

SYSTEM_HEAD = TEXT["SYSTEM_HEAD"]

FILE_TEMPLATE = TEXT["FILE_TEMPLATE"]


def system(rules: str) -> str:
    return SYSTEM_HEAD + ("\n## Quy tắc RTL (nguồn chuẩn)\n\n" + rules if rules else "")


def params_block(params: dict[str, str]) -> str:
    if not params:
        return "(không có tham số top-level)"
    return "\n".join(f"- {k} = {v}" for k, v in params.items())


def module_prompt(*, module: str, path: str, description: str, reqs: list[tuple[str, str]], params: dict[str, str],
                  children: list[tuple[str, str]], packages: list[str], spec_files: list[str], answers: dict[str, str],
                  settled: str, feedback: list[str], problems: str, existing: bool, top: bool) -> str:
    parts = [f"# Task: viết RTL cho `{module}` → `{path}`" + (" (module TOP)" if top else ""),
             f"\n**Tên module BẮT BUỘC là `{module}` y hệt** (khai báo `module {module} #(` hoặc `module {module} (`), vì "
             f"file `{path}` sẽ được dùng đúng tên này ở các step sau (sva bind, filelist, traceability) — không đổi "
             f"tên module theo một quy ước khác dù quy tắc đặt tên bên dưới gợi ý tiền tố nào khác; nếu tên gốc chưa "
             f"đúng tiền tố quy ước, đó là việc của step trước (parse), không phải việc của bước này.",
             f"\nMô tả (từ spec): {description or '(xem spec)'}"]
    parts.append("\n## Requirement module này phải implement (tag từng REQ-ID trong file)\n"
                 + ("\n".join(f"- {rid}: {text}" for rid, text in reqs) or "- (không có REQ gán riêng: implement theo spec)"))
    parts.append("\n## Tham số đã chốt (Gate 2, final_config.json)\n" + params_block(params))
    parts.append(f"\n## Spec\nĐọc: {', '.join(spec_files) or 'spec/'} (chỉ đọc phần liên quan tới module này).")
    if packages:
        parts.append("\n## Package đã có (import, đọc file để biết kiểu/enum)\n" + "\n".join(f"- {p}" for p in packages))
    if children:
        parts.append("\n## Module con mà module này instantiate (đã viết xong; ĐỌC file để lấy port chính xác, dùng named connection)\n"
                     + "\n".join(f"- `{n}` → {p}" for n, p in children))
    if answers:
        parts.append("\n## Câu trả lời của người dùng cho các câu hỏi trước\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()))
    if settled:
        parts.append(settled)
    if feedback:
        parts.append("\n## Yêu cầu thay đổi (áp dụng)\n" + "\n".join(f"- {f}" for f in feedback))
    if existing:
        parts.append(f"\nFile `{path}` đã tồn tại: ĐỌC rồi sửa phần cần sửa, không viết lại từ đầu nếu không cần.")
    if problems:
        parts.append("\n## Vấn đề Q3TUI còn thấy trong file (sửa hết)\n" + problems)
    parts.append("\n" + FILE_TEMPLATE)
    return "\n".join(parts)


def update_prompt(*, module: str, path: str, changes: list[str], answers: dict[str, str], feedback: list[str], settled: str,
                  problems: str) -> str:
    parts = [f"# Task: cập nhật RTL `{module}` → `{path}`",
             f"**Giữ nguyên tên module `{module}`** — không đổi theo quy ước đặt tên khác, file `{path}` phụ thuộc đúng tên này.",
             "Module này đã được viết từ phiên bản trước của đầu vào. CHỈ những thay đổi sau xảy ra — đọc file hiện tại, "
             "sửa đúng phần bị ảnh hưởng, giữ nguyên phần còn lại (và các tag REQ):",
             "\n".join(f"- {c}" for c in changes) or "- (không có thay đổi nhận diện được: kiểm tra lại file theo yêu cầu bên dưới)"]
    if answers:
        parts.append("\nCâu trả lời của người dùng:\n" + "\n".join(f"- {k}: {v}" for k, v in answers.items()))
    if settled:
        parts.append(settled)
    if feedback:
        parts.append("\nYêu cầu thay đổi:\n" + "\n".join(f"- {f}" for f in feedback))
    if problems:
        parts.append("\nVấn đề Q3TUI còn thấy (sửa hết):\n" + problems)
    return "\n".join(parts)


def fix_prompt(*, module: str, path: str, problems: str, code: str, reqs: list[str], req_text: dict[str, str] | None = None,
               keep_ports: list[str] | None = None) -> str:
    """A fresh small session: the problems and the current content — never the old session. `req_text`: the module is
    the user's imported RTL (conform it to the rules, keep what it does; tag the REQs where they are implemented)."""
    imported = ""
    if req_text is not None:
        imported = ("\nFile này là RTL của người dùng (import): GIỮ NGUYÊN chức năng, kiến trúc, timing (số chu kỳ) và tên module; "
                    "chỉ sửa để theo đúng quy tắc RTL (đổi tên tín hiệu/label/instance, khai báo, cấu trúc block…) và thêm tag REQ. "
                    "Đổi tên port của module con thì sửa luôn chỗ nối ở module cha khi được báo lỗi.\n"
                    "Requirement của module (tag `// REQ-xxx` ngay tại chỗ logic implement nó):\n"
                    + ("\n".join(f"- {r}: {t}" for r, t in req_text.items()) or "- (không có)") + "\n")
        if keep_ports:
            imported += ("Module TOP: KHÔNG đổi tên port (" + ", ".join(keep_ports[:60]) + ") — đó là giao diện trong spec và trong "
                         "test của người dùng.\n")
    return (f"# Task: sửa `{module}` → `{path}`\nQ3TUI kiểm tra file và còn thấy các vấn đề sau (kết quả công cụ là sự thật, "
            f"không tranh luận — sửa nguyên nhân, không che):\n{problems}\n{imported}\nREQ-ID phải còn được tag: {', '.join(reqs) or '(không có)'}\n\n"
            f"Nội dung hiện tại của file:\n```systemverilog\n{code}\n```\nSửa file bằng Edit/Write, chạy `check` tới khi `clean`, "
            "rồi trả về tóm tắt.")


PLAN_SYSTEM = TEXT["PLAN_SYSTEM"]


def plan_prompt(reqs: list[tuple[str, str]], spec_files: list[str], top: str | None) -> str:
    return ("# Task: lập kế hoạch module RTL\n\nRequirement:\n" + "\n".join(f"- {r}: {t}" for r, t in reqs)
            + f"\n\nSpec: {', '.join(spec_files) or 'spec/'}\n" + (f"Module top: `{top}`\n" if top else "")
            + "\nTrả về `top` và `modules` (name, description, req_ids, instances).")


def diagnostics_text(items: list[str], limit: int = 40) -> str:
    shown = items[:limit]
    return "\n".join(f"- {i}" for i in shown) + (f"\n- … (+{len(items) - limit} nữa)" if len(items) > limit else "")


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
