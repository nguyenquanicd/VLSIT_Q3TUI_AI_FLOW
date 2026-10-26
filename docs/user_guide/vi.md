# Hướng dẫn sử dụng Q3TUI (Tiếng Việt)

Bản khác: [English](en.md).

Q3TUI chạy **flow spec**: `spec → parse → config`, và **flow rtl** (`rules → csr → rtl → format → style_lint → smoke` sau cùng 3 step đó). Step testbench nằm trong flow `dv`; các step SVA, verify và doc đã bị gỡ. Tài liệu này hướng dẫn
cách cài đặt, chạy 1 project từ đầu tới cuối, hệ thống rule kiểm tra RTL, toàn bộ setting, và mọi lệnh/phím
bạn sẽ dùng hàng ngày.

- [1. Cài đặt](#1-cài-đặt)
- [2. Bắt đầu 1 project](#2-bắt-đầu-1-project)
- [3. Flow, từng bước](#3-flow-từng-bước)
- [4. Gate và review](#4-gate-và-review)
- [5. Rule coding RTL](#5-rule-coding-rtl)
- [6. Settings](#6-settings)
- [7. Lệnh và phím tắt](#7-lệnh-và-phím-tắt)
- [8. Input và output](#8-input-và-output)
- [9. Chat với assistant](#9-chat-với-assistant)
- [10. Flow RTL](#10-flow-rtl)

## 1. Cài đặt

```bash
git clone <repo này> && cd Q3TUI
uv sync                                                          # Python >= 3.11
mkdir -p ~/.q3tui && cp q3tui.example.yaml ~/.q3tui/q3tui.yaml   # cấu hình mặc định: model, module EDA
```

LLM dùng cơ chế xác thực chuẩn của Claude Agent SDK: đã login Claude Code sẵn, `ANTHROPIC_API_KEY`, hoặc
gateway/Bedrock/Vertex qua `llm.env` trong `q3tui.yaml`. Tool EDA (lint, synthesis, simulation) nạp qua
environment module (`tools.modules`, hoặc override riêng từng role) — role nào chưa cấu hình tool sẽ báo
**N/A**, không bao giờ âm thầm bỏ qua.

Muốn cài lệnh `q3tui` dùng toàn hệ thống thay vì chạy từ repo: `./install.sh` (dùng `uv tool install`;
`./install.sh --uninstall` để gỡ — không đụng tới `~/.q3tui` settings và các project của bạn).

## 2. Bắt đầu 1 project

1 project là bất kỳ thư mục nào có `spec/` (hoặc có `q3tui.yaml`). Q3TUI ghi deliverable vào chính thư mục
đó và lưu state riêng trong `.q3tui/` (LLM không bao giờ đọc thư mục này).

```bash
mkdir my_block && cd my_block
mkdir spec && vi spec/my_block_spec.md      # viết spec, hoặc bắt đầu từ intent (xem bên dưới)
q3tui                                       # mở TUI
```

**Điểm bắt đầu — spec có sẵn, hay chỉ có intent:**
- **File spec** trong `spec/` (Markdown, hoặc PDF) — `parse` đọc nguyên trạng.
- **Chỉ có intent** (`spec/intent.md`, vài câu mô tả bạn muốn thiết kế gì) — step `spec` tự soạn spec đầy đủ
  theo từng section, hỏi những gì còn mơ hồ, rồi mới chuyển cho `parse`.

### Màn hình

![Q3TUI dừng ở Gate 1: requirement chờ review](../images/tui-parse.svg)

| Vùng | Hiển thị gì |
|---|---|
| **Pipeline** (trái) | Mọi step: icon trạng thái (chú thích ngay bên dưới), số thứ tự (`1`–`9` để nhảy tới), mode của gate hoặc thứ step đang chờ (`review 2?`). |
| **Step view** (trên, phải) | Kết quả của step đang chọn, chia tab (Requirements / Parameters / Files / Questions / …). Banner phía trên cho biết step cần gì từ bạn. |
| **Activity** (dưới, phải) | Log trực tiếp của lần chạy, và câu trả lời của assistant. `z` để phóng to. |
| **Thanh lệnh / chat** | `/command` (Tab để gợi ý, ↑/↓ lịch sử), hoặc gõ chữ thường để chat với assistant (`i` để focus). |
| **Footer** | Phím tắt của vùng đang focus; `?` liệt kê tất cả. |

Kéo các đường chia (hoặc `ctrl+mũi tên`) để đổi kích thước; kích thước được nhớ theo project.

## 3. Flow, từng bước

```text
spec ─► parse ─► config
 human   Gate 1   Gate 2
         human    auto
```

| Step | Làm gì | Gate |
|---|---|---|
| **spec** | Soạn (hoặc nhận) đặc tả IP. | human |
| **parse** | Trích requirement (REQ-ID), parameter, bản đồ module từ spec; đánh dấu chỗ mơ hồ. | **1 · human** |
| **config** | Chốt giá trị parameter theo change request của bạn; check constraint lúc elaboration. | 2 · auto (tự ký khi constraint pass) |

RTL do flow `rtl` tạo (`pipeline.flow: rtl`, `q3tui --flow rtl run`): flow này dùng lại 3 step trên và thêm rules, CSR, RTL,
format + style lint bằng Verible, và smoke test (docs/spec/rtl-flow.md).

Testbench do flow `dv` tạo (`pipeline.flow: dv`, `q3tui --flow dv run`): cùng 3 step đầu rồi thêm `testplan` (verification contract và các test, kèm các open issue bạn trả lời),
`tb` (testbench UVM dựng từ VIP có sẵn, chỉ từ spec, không bao giờ từ RTL cho đến phase cuối, khi RTL được tích hợp làm DUT) và `simulate` (chạy smoke và directed test, mọi status đọc từ log
simulator); option ở `pipeline.step_options.tb.uvm_vip`, report ở `reports/` (docs/spec/dv-flow.md, docs/spec/dut-dv.md, ví dụ `examples/axi4_sram_ctrl`).
SVA, verify và document sinh tự động vẫn đã bị gỡ; chưa import được file testbench.

**Update, không chạy lại từ đầu:** sửa 1 requirement, 1 parameter, hay gửi change request chỉ khiến đúng
phần bị ảnh hưởng chạy lại — các step trước và approval đã duyệt vẫn giữ nguyên, trừ khi thay đổi lan tới đó.

## 4. Gate và review

Gate là điểm dừng chờ quyết định của bạn. Mỗi gate có 1 **mode**:

| Mode | Hành vi |
|---|---|
| `human` | Luôn dừng; chỉ bạn mới approve được (`a` / `/approve`). |
| `auto` | Tự approve khi không còn câu hỏi *blocking* — item review chưa confirm (bên dưới) không tính vào điều kiện này. |
| `auto_answer` | Chạy không giám sát: tự approve, mọi câu hỏi (kể cả blocking) lấy giá trị mặc định. |
| `none` | Không có gate. |

Đổi mode của gate: `g` (picker) hoặc `/gate <step> <mode>`. `pipeline.auto_approve` / `pipeline.auto_answer`
(trong settings, hoặc `/autoapprove` `/autoanswer`) override mode của **mọi** gate cùng lúc.

**2 thứ khiến gate đang chờ**, hiện ngay trên banner kết quả của step:

- **Blocking question** — quyết định thật sự chỉ bạn mới trả lời được (requirement mơ hồ, parameter xung
  đột...). Mở tab Questions (`o`) và trả lời.
- **Item review chưa confirm** — requirement bị đánh dấu, hoặc parameter đổi khỏi default mà
  chưa ai xem qua. Những cái này **không** chặn gate `auto`/`auto_answer` (gate tự ký, item vẫn mở), nhưng **có** chặn `Approve` (`a`) thường trên gate `human`, trừ khi bạn bật
  `pipeline.auto_confirm_reviews`.

![Hộp thoại review của gate](../images/tui-gate.svg)

`a` mở hộp thoại review ở trên: approve, trả lời câu hỏi còn mở, edit kết quả, yêu cầu thay đổi, hoặc để sau.

**Review và confirm** (theo từng step, trong tab panel riêng). Phím in hoa = **Shift + chữ đó**, là phím
khác hẳn chữ thường (vd `C` không phải `c`):

| Step / tab | Phím | Hành động |
|---|---|---|
| parse → Requirements | `c` | Confirm requirement đang highlight (đã đọc, ổn) |
| | `u` | Đưa về lại pending |
| | `C` (Shift+c) | Confirm hết mọi requirement bị đánh dấu |
| config → Parameters | `c` / `u` / `C` (Shift+c) | Tương tự, cho parameter đổi khỏi default |


`/approve <step> --force` và `/approve all` bỏ qua cả blocking question *lẫn* review chưa confirm — dùng khi
bạn đã tự đánh giá là ổn, không nên dùng theo thói quen.

## 5. Rule coding RTL

Mỗi stage RTL đọc 1 **file rule** (Markdown thuần, đúng văn bản 1 kỹ sư sẽ đọc) và 1 **bộ check
deterministic** (code, không qua LLM) kiểm tra những gì kiểm tra được một cách máy móc — pattern đặt tên,
construct bị cấm, độ dài tên, tên file khớp tên module, v.v. LLM được đưa rule để đọc; nó không có quyền
quyết định mình có tuân thủ hay không — code kiểm tra lại.

- **Baseline mặc định:** `q3tui/flows/rtl/rtl/rules/rtl_rule_default.md` — rule synthesis
  ASIC (đặt tên, quy ước port DFT, construct cấm P1–P29, checklist sẵn sàng tapeout).
- **Thêm rule riêng:** đặt file rule (vd `my_rules.md`) vào root project, liệt kê cùng với bản mặc định
  trong `pipeline.step_options.rtl.rules` ở `q3tui.yaml`:
  ```yaml
  pipeline:
    step_options:
      rtl:
        rules: [rtl_rule_default.md, my_rules.md]
  ```
  File trong project có **cùng tên** với bản mặc định sẽ ghi đè hoàn toàn (được đọc trước).
- **Những gì đang được enforce bằng code:** tiền tố đặt tên (`i_`/`o_`/`reg_`/`w_`/pattern module/instance/
  parameter), độ dài tên (30 ký tự, profile mặc định), cấm system task kiểu testbench (`$display`,
  `$finish`, `$random`...) — `$error` chỉ được phép làm ngoại lệ check parameter lúc elaboration —
  `casex`, `defparam`, `inout` ngoài pad wrapper, kiểu/literal signed, kiểu two-state, `` `define ``/
  `` `undef `` và nhiều hơn nữa. Những gì không kiểm máy móc được (vd bảng viết tắt, không có combinational
  loop) thì vẫn chỉ là hướng dẫn LLM tự tuân theo từ nội dung rule.

## 6. Settings

Mở bằng `,` (dialog Settings) hoặc `/settings [tab]`; mọi setting đều đọc/set được qua assistant
(`get_settings` / `set_settings`). Thêm `--save` vào lệnh `/command`, hoặc bỏ tick "Only for this session"
trong dialog, để lưu vào `q3tui.yaml`; để tick (hoặc không thêm `--save`) nếu chỉ muốn thử trong session này.

Dialog có 1 tab cho mỗi mảng: **General**, **Gates**, **LLM**, **Models per step**, **Spec**, **RTL**.

### General

| Setting | Kiểu | Chức năng |
|---|---|---|
| `pipeline.auto_approve` | bool | Mọi gate tự approve; gate còn blocking question vẫn dừng lại hỏi. |
| `pipeline.auto_answer` | bool | Chạy không giám sát: mọi gate tự approve, mọi câu hỏi (kể cả blocking) lấy default, các đề xuất được chấp nhận như đề xuất. |
| `pipeline.max_fix_iterations` | int | Số vòng check → fix → check của một step tối đa trước khi dừng và trả lại cho bạn. |
| `pipeline.escalate_after_repeats` | int | 1 bug bị phân loại giống nhau liên tiếp bao nhiêu lần thì chuyển cho bạn thay vì tự fix tiếp. |
| `pipeline.auto_confirm_reviews` | bool | Tự confirm item review của step (requirement bị đánh dấu, parameter đổi default) mà không hỏi. Gate human vẫn chờ `Approve`; `Approve` không còn từ chối vì item chưa confirm. Hẹp hơn auto-approve/auto-answer ở trên — gate vẫn dừng, chỉ bỏ review từng item. |
| `tui.icons` | choice | Bộ icon trạng thái: `unicode` (✔ ⚑ ◐ …), `nerd` (cần font "… Nerd Font Mono"), `ascii` (dùng được mọi font). |
| `tui.language` | choice | Ngôn ngữ UI chrome (legend, banner, dialog) và nội dung pipeline sinh ra (spec, comment RTL, câu hỏi, docs): `en` / `vi` / `ko`. Chat assistant không bị ảnh hưởng — luôn tự theo ngôn ngữ bạn gõ. |

### Gates

Mỗi step có gate review được 1 ô chọn mode: **human** (dừng chờ bạn) / **auto** (tự approve; blocking
question vẫn chặn) / **auto-answer** (không giám sát) / **none** (không có gate). Tương đương bấm `g` trên 1
step, hoặc `/gate <step> <mode>` — tab này chỉ hiện mode của mọi step cùng lúc. Xem [§4](#4-gate-và-review)
để biết từng mode làm gì.

### LLM

| Setting | Kiểu | Chức năng |
|---|---|---|
| `llm.model` | choice | Model mặc định cho mọi stage từ stage tiếp theo trở đi: Opus 5.5 (chất lượng cao nhất, mặc định), Sonnet 5.5 (nhanh/rẻ hơn), Haiku 4.5 (rẻ nhất, không có effort), Fable 5.1 (mạnh nhất, đắt nhất). |
| `llm.effort` | choice, nullable | Mức độ suy luận: `low` / `medium` / `high` / `xhigh` / `max`. Để trống với Haiku 4.5 (không có effort). |
| `llm.thinking_budget` | int, nullable | Chỉ cho model không có effort (Haiku 4.5): giới hạn token thinking. `0` = tắt, để trống = mặc định của model. |
| `llm.max_budget_usd` | float, nullable | Dừng stage nào tốn quá số tiền này (để trống = không giới hạn). |
| `llm.tool_output_limit` | int | Số ký tự 1 tool call được trả về; kết quả được gửi lại mỗi turn sau, nên cái dài bị cắt (giữ đầu + cuối). |
| `llm.stage_token_budget` | int, nullable | Cảnh báo khi 1 stage đọc quá số token input này (tính cả cache; để trống = không bao giờ cảnh báo). |
| `llm.timeout_s` | int, nullable | Giới hạn thời gian 1 LLM stage (để trống = không giới hạn, giống Claude Code). |
| `llm.fallback_model` | text, nullable | Dùng khi model chính quá tải (để trống = không có). |

### Models per step

Mỗi step (spec, parse, config, rtl) và assistant có 1 cặp `model` + `effort` riêng —
để trống thì dùng mặc định ở tab LLM. Cho phép trộn model, vd
Opus chỗ cần chính xác, Haiku cho việc máy móc/khối lượng lớn.

### Spec

| Setting | Kiểu | Chức năng |
|---|---|---|
| `spec.self_review` | bool | 1 lượt review thứ 2 kiểm tra bản draft có chỗ mơ hồ không, chỉ trả về phần thay đổi. Tắt = draft nhanh hơn, không check mơ hồ. |

### RTL

| Setting | Kiểu | Chức năng |
|---|---|---|
| `rtl.parallel` | int | Số module viết cùng lúc. |
| `rtl.max_fix_attempts` | int | Số vòng thêm khi lint hoặc synthesis vẫn fail. |
| `rtl.max_turns` | int | Số turn agent cho 1 module (viết, compile, fix). |

Rule coding là file rule riêng của flow rtl, xem [§5](#5-rule-coding-rtl).

## 7. Lệnh và phím tắt

Bấm `/` để gõ lệnh (Tab/→ để auto-complete); cùng hành động đó có phím tắt khi đang focus vào step view.
Phím 1 chữ cái viết **IN HOA** nghĩa là **Shift + chữ đó** — là phím khác hẳn với chữ thường, và trong flow
này 2 phím (in hoa/thường) không bao giờ làm cùng 1 việc.

| Hành động | Phím | Lệnh |
|---|---|---|
| Chạy pipeline (có thể chọn range) | `r` | `/run [from] [to]` |
| Dừng step đang chạy / dừng reply của assistant | `x` | `/stop` |
| Mở dialog review gate | `a` | — |
| Approve 1 gate, hoặc mọi gate đang chờ | `A` (Shift+a) | `/approve <step>|all [--force]` |
| Gửi change request cho 1 step | — | `/change <step> <text>` |
| Trả lời câu hỏi | — | `/answer <id> <text>` / `/answer all` |
| Mở tab Questions | `o` | — |
| Đổi mode của gate | `g` | `/gate <step> <mode> [--session]` |
| Liệt kê toàn bộ gate | — | `/gates` |
| Flow editor | `F` (Shift+f) | `/flow` |
| Skill editor | `K` (Shift+k) | `/skill` |
| Prompt editor | `P` (Shift+p) | `/prompts` |
| Tuỳ chỉnh 1 step (view/pass/notes) | — | `/step <step> view|pass|notes [text]` |
| Reset output của step (hoặc toàn bộ) | `R` (Shift+r) | `/reset <step>|all [--only]` |
| Sửa file hiện tại | `e` | — |
| Comment lên text đã chọn / dòng đang highlight | `C`\* (Shift+c) | — |
| Đổi model / mức suy luận | `m` | `/model [id]` / `/effort <level>` |
| Bật/tắt self-review / auto-approve / auto-answer / auto-confirm | — | `/specreview` `/autoapprove` `/autoanswer` `/autoconfirm on|off` |
| Dialog Settings | `,` (dấu phẩy) | `/settings [tab]` |
| Thống kê sử dụng | `s` | `/stats [reset]` |
| Chi phí LLM tới giờ | — | `/cost [reset]` |
| Chọn step 1–9 | `1`–`9` | — |
| Resize pane | ctrl+arrow, hoặc kéo chuột | — |
| Zoom pane chat/activity | `z` | — |
| Focus ô nhập chat | `i` | — |
| Bắt đầu hội thoại assistant mới | — | `/newchat` |
| Thoát | ctrl+q | `/quit` |
| Help | `?` (shift+/) | `/help` |

\* `C` (Shift+c) là **Comment** ở toàn app, nhưng là **Confirm all** khi đang focus tab Requirements/
Parameters — xem [§4](#4-gate-và-review). Các phím review riêng từng step (`c`/`u`/`C` để
confirm) nằm trong bảng đó, không lặp lại ở đây.

## 8. Input và output

```text
spec/<ip>_spec.md                       input của bạn (hoặc output của step spec, từ intent)
schemas/structured_spec.json            parse    — Gate 1
schemas/final_config.json               config   — Gate 2
schemas/synth_report.json               rtl
reviews/{parse,config}_reviews.json     review từng item của bạn (giữ nguyên qua reset)
src/rtl/*.sv, filelist.f                    rtl (flow rtl)
config/{tools,pdks}.json, rtl_rules.md   cấu hình của bạn
constraints/sdc/                        SDC (flow fpga / asic / sanity)
reports/  build/                        kết quả dễ đọc / output công cụ dùng một lần
.q3tui/…                                chỉ nội bộ — LLM không bao giờ đọc
```

## 9. Chat với assistant

Gõ bất kỳ thứ gì không bắt đầu bằng `/command` vào ô nhập dưới cùng, nó sẽ gửi tới assistant — assistant có
tool cho mọi hành động trong tài liệu này (chạy step, trả lời câu hỏi, đổi mode gate, review requirement,
đổi setting...) và hỏi xác nhận trước khi làm gì có tính phá huỷ (reset, bật chế độ chạy
không giám sát). Assistant luôn trả lời bằng đúng ngôn ngữ bạn gõ, không phụ thuộc vào `tui.language`.

## 10. Flow RTL

`pipeline.flow: rtl` (trong `q3tui.yaml`, hoặc `q3tui --flow rtl`) chạy riêng phần sinh RTL, kiểm tra và smoke test; verification để flow khác lo. Thiết kế chi tiết: `docs/spec/rtl-flow.md`.

```
spec → parse → config → rules → csr → rtl → format → style_lint → smoke
```

1. **Rule của bạn.** `q3tui act rtl_rules_init` tạo `config/rtl_rules.md`: mỗi rule một bullet, `- **ID** nội dung`. File này được đọc trước rule mặc định của Q3TUI (cùng id = ghi đè).
   Step `rules` kiểm tra mâu thuẫn (dừng và chỉ ra đúng chỗ cần sửa) rồi viết flag Verible vào `src/rtl/style_lint/`.
2. **Thanh ghi.** Nếu spec có register map, step `csr` soạn `src/rtl/csr/csr_map.json` và sinh khối thanh ghi bằng APB-CSR-Generator (chạy `q3tui act csr_tool_install` một lần;
   hoặc đặt `csr.tool_dir`, `env.CSR_GEN_HOME` của role `csr_gen` trong `config/tools.json`, hoặc `$CSR_GEN_HOME`). Muốn đổi thanh ghi thì sửa `csr_map.json`: sau lần soạn đầu file này là của bạn. `csr.mode: off` để bỏ qua.
3. **RTL, format, style lint.** `rtl` theo rule; `format` chỉ đổi khoảng trắng (hỏi trước khi đụng file bạn đã sửa tay); `style_lint` chạy Verible và gửi lỗi vi phạm về `rtl`.
   Chuỗi này tự lặp, tối đa `style_lint.max_rounds` vòng.
4. **Smoke test.** Testbench nhỏ viết chỉ từ spec, compile và chạy (role `sim`, `run`). Nếu fail thì được triage và sửa, hoặc thành câu hỏi cho bạn.

Công cụ: Verible (`verible_lint`, `verible_format`) và generator CSR có cấu hình mặc định theo flow; `sim` / `run` / `lint` / `synth` lấy từ `config/tools.json` của bạn (preset Synopsys nếu chưa có).
Thiếu tool thì step chờ và nêu tên role. **Skill:** các stage LLM đọc playbook (`config/skills/<tên>/SKILL.md`); đặt skill của bạn trong `<project>/config/skills/` để thêm hoặc thay.
`q3tui act style_show` và `q3tui act csr_show` in ra những gì flow đã quyết định.

**Vòng sửa lỗi CDC / RDC.** `q3tui --flow sanity loop [--owner rtl] [--max-rounds N] [--max-cost USD]` chạy phân tích Sanity, chuyển các fix mà review đề xuất sang flow rtl, để flow đó
cập nhật RTL rồi phân tích lại, cho đến khi hết violation, một vòng không tốt hơn vòng tốt nhất, không còn gì để chuyển, một flow bị lỗi hoặc chạm giới hạn chi phí. RTL của vòng tốt nhất được giữ lại;
`q3tui loop --restore-best` đặt nó về chỗ cũ. `q3tui act sanity_handoff` cũng thêm các lệnh SDC thuần của review vào `constraints/sdc/extra.tcl` (khi SDC sinh từ `sdc.json`).
