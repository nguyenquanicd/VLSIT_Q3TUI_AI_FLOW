"""The project's `tui.language` setting also sets the language every pipeline step's LLM output (spec text,
comments, questions, documents) defaults to — appended to every stage's prompt in `StepContext.llm`
(pipeline/base.py). The TUI assistant chat is excluded (it mirrors whatever language the user writes in instead;
see assistant.py SYSTEM). `q3tui/tui/i18n.py` is a separate, UI-only table for the TUI's own chrome text.
"""

from __future__ import annotations

INSTRUCTION = {
    "vi": "Viết toàn bộ nội dung bạn sinh ra (giải thích, comment, câu hỏi, tài liệu) bằng tiếng Việt — tên định "
          "danh/biến/tham số trong code vẫn giữ đúng quy ước đặt tên bắt buộc (thường là tiếng Anh theo rule).",
    "ko": "생성하는 모든 내용(설명, 주석, 질문, 문서)을 한국어로 작성하세요 — 코드 식별자/변수명/파라미터는 "
          "필수 명명 규칙(보통 영어)을 그대로 유지합니다.",
    "en": "Write all content you generate (explanations, comments, questions, documents) in English.",
}


def instruction(lang: str) -> str:
    return INSTRUCTION.get(lang, INSTRUCTION["en"])
