"""Task input of the `parse` step — adapted from <step>/md/spec_parser.md (+ spec_parser_authoring_rules.md). The
interactive parts of the original (asking, gates, printing tables) are the engine's and the TUI's job here."""

from __future__ import annotations

from pathlib import Path

from q3tui.flows import skill_sections

# the stage system prompts are the named sections of md/skill.md (this file only assembles the per-task input)
TEXT = skill_sections(Path(__file__).parent / "md" / "skill.md")

SYSTEM = TEXT["SYSTEM"]


def design_block(design: str) -> str:
    """The user's imported RTL (names and hierarchy read by code): the module list must be exactly this."""
    if not design:
        return ""
    return ("\n## RTL đã có (người dùng import; tên và phân cấp do code đọc từ file — sự thật)\n"
            "`modules` và `top_module` PHẢI dùng đúng các module này (đúng tên, đúng phân cấp); `rtl_modules` của mỗi "
            "requirement chỉ được là các tên này. Không thêm module mới.\n" + design + "\n")


def first_prompt(spec_block: str, settled: str, feedback: list[str], design: str = "") -> str:
    fb = ("\n\nYêu cầu thay đổi của người dùng (áp dụng):\n" + "\n".join(f"- {f}" for f in feedback)) if feedback else ""
    return (f"Phân tích spec sau và trả về kết quả có cấu trúc (toàn bộ requirement, parameter, constraint, module).\n\n{spec_block}\n"
            f"{design_block(design)}{settled}{fb}")


def update_prompt(spec_block: str, previous: str, answers: list[tuple[str, str, str]], settled: str, feedback: list[str],
                  design: str = "") -> str:
    """Spec (or decisions) changed since the last parse: the previous result is given; return the complete updated result,
    keeping the `id` of every requirement that is unchanged."""
    ans = "\n".join(f"- {qid} ({q}) → {a}" for qid, q, a in answers)
    return ("Spec đã thay đổi hoặc người dùng đã quyết định; cập nhật kết quả phân tích TRƯỚC ĐÓ. Trả về kết quả ĐẦY ĐỦ sau cập nhật: "
            "giữ nguyên `id` của mọi requirement không đổi (text/ý nghĩa), requirement mới để id = \"\", requirement đã bị xoá khỏi spec thì bỏ. "
            "Đừng viết lại những requirement không bị ảnh hưởng.\n\n"
            f"## Kết quả trước đó\n{previous}\n\n## Spec hiện tại\n{spec_block}\n" + design_block(design)
            + (f"\n## Câu trả lời của người dùng (áp dụng, chấm lại score; không hỏi lại)\n{ans}\n" if ans else "")
            + (f"\nYêu cầu thay đổi của người dùng (áp dụng):\n" + "\n".join(f"- {f}" for f in feedback) + "\n" if feedback else "")
            + settled)
