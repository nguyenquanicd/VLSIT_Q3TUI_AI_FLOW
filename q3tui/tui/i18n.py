"""The TUI's own chrome text (setting `tui.language`): the status legend and the result banners above each step
view. Nothing else is translated here — LLM-generated content (spec, RTL, docs, chat replies) is controlled by each
step's own prompt, not by this setting, and keeps whatever language that prompt uses regardless of `tui.language`.

Keys are plain English fallback text; `t()` looks the key up in the active language, falling back to English (and
then the key itself) when a translation is missing, so a partially translated language never shows a blank string.
"""

from __future__ import annotations

LANGS = ("en", "vi", "ko")

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "legend.approved": "approved", "legend.not_reviewed": "not reviewed", "legend.out_of_date": "out of date",
        "legend.yours": "yours", "legend.running": "running", "legend.failed": "failed",
        "legend.no_result": "no result", "legend.planned": "planned", "legend.edited": "edited by you",
        "banner.running": "RUNNING", "banner.running_detail": "a new version is being generated",
        "banner.showing_previous": "showing the previous result",
        "banner.last_run_failed": "LAST RUN FAILED", "banner.retry": "r to retry",
        "banner.showing_last_good": "showing the last good result",
        "banner.planned": "PLANNED", "banner.showing_your_files": "showing your files",
        "banner.waiting_for_input": "WAITING FOR INPUT",
        "banner.no_result_yet": "NO RESULT YET", "banner.press_r_to_run": "press r to run",
        "banner.showing_older_result": "showing an older result",
        "banner.out_of_date": "OUT OF DATE", "banner.inputs_changed": "its inputs changed",
        "banner.showing_old_result": "showing the old result", "banner.update_r": "r to update",
        "banner.was_approved": "was approved", "banner.not_reviewed_short": "not reviewed",
        "banner.yours": "YOURS", "banner.yours_detail": "provided by you, used as-is",
        "banner.question_waiting": "QUESTION WAITING", "banner.blocking_questions": "{n} blocking question(s): "
        "open the Questions tab (o) and answer",
        "banner.confirm_needed": "CONFIRM NEEDED",
        "banner.items_not_reviewed": "{n} item(s) not reviewed yet ({items}): confirm them (c), or C to confirm all",
        "banner.not_reviewed": "NOT REVIEWED", "banner.review_or_change": "a to review/approve, or request changes",
        "banner.approved": "APPROVED", "banner.done": "DONE",
        "banner.edited_by_you": "EDITED", "banner.edited_detail": "by you: {files}",
        "picker.other": "Other…", "picker.type_own": "type your own answer", "picker.add_typed": "add a typed answer",
        "picker.hint_multi": "↑/↓ move · Space toggles · Enter picks the highlighted (or the ticked) · ctrl+s confirms · Esc cancels",
        "picker.hint_single": "↑/↓ or number: choose · Enter: pick · Esc: cancel",
        "picker.hint_other_suffix": " · Other… lets you type",
        "picker.cancel": "Cancel", "picker.toggle": "Toggle", "picker.confirm": "Confirm",
        "picker.recommended": " (recommended)",
        "q.col_id": "ID", "q.col_status": "Status", "q.col_question": "Question", "q.col_answer": "Answer / assumption",
        "q.summary": "{open} open · {closed} closed{hidden} — Enter to answer (or change an answer) · h show/hide closed",
        "q.hidden_suffix": " (hidden)", "q.target_suffix": " · → spec / → req / → arch: your answer goes to that step",
        "q.blocking": "blocking", "q.open": "open", "q.answered": "answered", "q.default": "default", "q.old": " ·old",
        "q.toggle_closed": "Show/hide closed",
    },
    "vi": {
        "legend.approved": "đã duyệt", "legend.not_reviewed": "chưa review", "legend.out_of_date": "đã cũ",
        "legend.yours": "của bạn", "legend.running": "đang chạy", "legend.failed": "lỗi",
        "legend.no_result": "chưa có kết quả", "legend.planned": "dự kiến", "legend.edited": "bạn đã sửa",
        "banner.running": "ĐANG CHẠY", "banner.running_detail": "đang sinh phiên bản mới",
        "banner.showing_previous": "đang hiện kết quả trước đó",
        "banner.last_run_failed": "LẦN CHẠY TRƯỚC LỖI", "banner.retry": "r để chạy lại",
        "banner.showing_last_good": "đang hiện kết quả tốt gần nhất",
        "banner.planned": "DỰ KIẾN", "banner.showing_your_files": "đang hiện file của bạn",
        "banner.waiting_for_input": "ĐANG CHỜ ĐẦU VÀO",
        "banner.no_result_yet": "CHƯA CÓ KẾT QUẢ", "banner.press_r_to_run": "bấm r để chạy",
        "banner.showing_older_result": "đang hiện kết quả cũ hơn",
        "banner.out_of_date": "ĐÃ CŨ", "banner.inputs_changed": "đầu vào đã đổi",
        "banner.showing_old_result": "đang hiện kết quả cũ", "banner.update_r": "r để cập nhật",
        "banner.was_approved": "đã được duyệt", "banner.not_reviewed_short": "chưa review",
        "banner.yours": "CỦA BẠN", "banner.yours_detail": "do bạn cung cấp, dùng nguyên trạng",
        "banner.question_waiting": "CÒN CÂU HỎI", "banner.blocking_questions": "{n} câu hỏi cần trả lời: "
        "mở tab Questions (o) và trả lời",
        "banner.confirm_needed": "CẦN CONFIRM",
        "banner.items_not_reviewed": "{n} mục chưa review ({items}): confirm từng cái (c), hoặc C để confirm hết",
        "banner.not_reviewed": "CHƯA REVIEW", "banner.review_or_change": "a để review/duyệt, hoặc yêu cầu thay đổi",
        "banner.approved": "ĐÃ DUYỆT", "banner.done": "XONG",
        "banner.edited_by_you": "ĐÃ SỬA", "banner.edited_detail": "bởi bạn: {files}",
        "picker.other": "Khác…", "picker.type_own": "tự gõ câu trả lời", "picker.add_typed": "thêm câu trả lời tự gõ",
        "picker.hint_multi": "↑/↓ di chuyển · Space tick/bỏ tick · Enter chọn dòng đang highlight (hoặc các dòng đã tick) · ctrl+s xác nhận · Esc huỷ",
        "picker.hint_single": "↑/↓ hoặc số: chọn · Enter: chọn · Esc: huỷ",
        "picker.hint_other_suffix": " · Khác… để tự gõ",
        "picker.cancel": "Huỷ", "picker.toggle": "Tick/bỏ tick", "picker.confirm": "Xác nhận",
        "picker.recommended": " (đề xuất)",
        "q.col_id": "ID", "q.col_status": "Trạng thái", "q.col_question": "Câu hỏi", "q.col_answer": "Trả lời / giả định",
        "q.summary": "{open} chưa trả lời · {closed} đã xong{hidden} — Enter để trả lời (hoặc đổi câu trả lời) · h ẩn/hiện câu đã xong",
        "q.hidden_suffix": " (đang ẩn)", "q.target_suffix": " · → spec / → req / → arch: câu trả lời sẽ gửi tới step đó",
        "q.blocking": "chặn", "q.open": "mở", "q.answered": "đã trả lời", "q.default": "mặc định", "q.old": " ·cũ",
        "q.toggle_closed": "Ẩn/hiện câu đã xong",
    },
    "ko": {
        "legend.approved": "승인됨", "legend.not_reviewed": "미검토", "legend.out_of_date": "오래됨",
        "legend.yours": "직접 제공", "legend.running": "실행 중", "legend.failed": "실패",
        "legend.no_result": "결과 없음", "legend.planned": "예정", "legend.edited": "직접 수정함",
        "banner.running": "실행 중", "banner.running_detail": "새 버전을 생성하는 중",
        "banner.showing_previous": "이전 결과를 표시 중",
        "banner.last_run_failed": "마지막 실행 실패", "banner.retry": "r로 재시도",
        "banner.showing_last_good": "마지막으로 성공한 결과를 표시 중",
        "banner.planned": "예정됨", "banner.showing_your_files": "직접 작성한 파일을 표시 중",
        "banner.waiting_for_input": "입력 대기 중",
        "banner.no_result_yet": "결과 없음", "banner.press_r_to_run": "r을 눌러 실행",
        "banner.showing_older_result": "이전 결과를 표시 중",
        "banner.out_of_date": "오래됨", "banner.inputs_changed": "입력이 변경됨",
        "banner.showing_old_result": "이전 결과를 표시 중", "banner.update_r": "r로 업데이트",
        "banner.was_approved": "승인되었음", "banner.not_reviewed_short": "미검토",
        "banner.yours": "직접 제공", "banner.yours_detail": "직접 제공, 그대로 사용",
        "banner.question_waiting": "질문 대기 중", "banner.blocking_questions": "차단 질문 {n}개: "
        "Questions 탭(o)을 열어 답변하세요",
        "banner.confirm_needed": "확인 필요",
        "banner.items_not_reviewed": "미검토 항목 {n}개 ({items}): 개별 확인(c) 또는 C로 전체 확인",
        "banner.not_reviewed": "미검토", "banner.review_or_change": "a로 검토/승인, 또는 변경 요청",
        "banner.approved": "승인됨", "banner.done": "완료",
        "banner.edited_by_you": "수정됨", "banner.edited_detail": "직접 수정: {files}",
        "picker.other": "기타…", "picker.type_own": "직접 답변 입력", "picker.add_typed": "직접 입력한 답변 추가",
        "picker.hint_multi": "↑/↓ 이동 · Space 선택 토글 · Enter 강조된 항목(또는 선택된 항목) 선택 · ctrl+s 확인 · Esc 취소",
        "picker.hint_single": "↑/↓ 또는 숫자: 선택 · Enter: 선택 · Esc: 취소",
        "picker.hint_other_suffix": " · 기타…로 직접 입력 가능",
        "picker.cancel": "취소", "picker.toggle": "토글", "picker.confirm": "확인",
        "picker.recommended": " (추천)",
        "q.col_id": "ID", "q.col_status": "상태", "q.col_question": "질문", "q.col_answer": "답변 / 가정",
        "q.summary": "미답변 {open}개 · 완료 {closed}개{hidden} — Enter로 답변(또는 답변 수정) · h로 완료 항목 표시/숨김",
        "q.hidden_suffix": " (숨김)", "q.target_suffix": " · → spec / → req / → arch: 답변이 해당 step으로 전달됩니다",
        "q.blocking": "차단", "q.open": "미답변", "q.answered": "답변됨", "q.default": "기본값", "q.old": " ·이전",
        "q.toggle_closed": "완료 항목 표시/숨김",
    },
}

_current = "en"


def set_lang(name: str) -> None:
    global _current
    _current = name if name in LANGS else "en"


def t(key: str, **kwargs) -> str:
    s = TEXT.get(_current, {}).get(key) or TEXT["en"].get(key, key)
    return s.format(**kwargs) if kwargs else s
