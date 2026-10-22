# Release notes: DUT-DV `uvm_vip` testbench backend (V0.1)

Bilingual: English first, Vietnamese below (Tiếng Việt ở phần sau).

---

## English

### Summary
An optional backend for the `tb` step of the dv flow. From the approved spec, the approved config and an existing AXI VIP it builds a UVM testbench,
integrates the released RTL as the DUT, compiles with VCS and runs a smoke test. Select it with `backend: uvm_vip` in `q3tui.yaml`
(`pipeline.step_options.tb`). The default `plain_sv` backend is unchanged.

Core rule: the specification is the golden behavior; the RTL is only the DUT. RTL is read only in a separate integration phase, for structure
(top, ports, parameters, files). A spec/RTL mismatch is reported, never adapted. Unspecified behavior stays OPEN. No fake simulator evidence.

### Release status
| Item | Status |
|---|---|
| TC-001 `dut_smoke_test` (VCS X-2025.06, real RTL and VIP) | **PASS**: 11 checks, 0 mismatches, UVM_ERROR 0, UVM_FATAL 0 |
| TC-002..006, 008, 009, 010, 012 (incl. the 4 KB boundary test) | **PASS** (live agent run, about 0.7 USD) |
| TC-011 timing | PASS but does **not** verify timing (SBR-007 makes no check) |
| TC-007 backpressure | **INCOMPLETE** (0 checks; not evidence) |
| SBR-006, 007, 009 checks; COV-001..009; assertions | **todo** (not checked / not implemented) |
| Full regression | 349 passed, 4 failed (pre-existing: missing `examples/sync_fifo/fifo.f`) |

### Modified files (4)
| File | What changed | Why |
|---|---|---|
| `q3tui/flows/dv/tb/step.py` | Dispatches to the new code when `backend == uvm_vip`; checks VIP/RTL inputs before a run; refreshes the review report on approve. | Lets the new backend plug into the `tb` step without touching the default path. |
| `q3tui/steps/vlsit/artifacts.py` | Registers 5 artifacts: `golden_manifest`, `verification_contract`, `tb_architecture`, `vip_integration`, `dut_integration`. | The engine must know the new step outputs. |
| `pyproject.toml` | Adds the pytest marker `vcs`. | Tests that need real VCS can be selected (`-m vcs`) and skip when VCS is absent. |
| `.gitignore` | Un-ignores `examples/axi4_sram_ctrl/.q3tui/pipeline.json`. | The example ships its recorded run state so a re-run needs no LLM. |

### Added files
| Path | Function |
|---|---|
| `q3tui/flows/dv/tb/uvm/__init__.py` | Orchestrates the phases and records state. |
| `.../uvm/spec_facts.py` | Extracts ports, assumptions and original names from the spec. |
| `.../uvm/contract.py` | Verification contract model, validation, freeze by hash. |
| `.../uvm/intent.py`, `task.py` | `tb_intent` LLM stage that fills the semantic columns, and its prompt. |
| `.../uvm/review.py` | Review ledger: user-answered, accepted default, auto default, pending. |
| `.../uvm/arch.py` | TB architecture. |
| `.../uvm/scaffold.py`, `svgen.py` | Deterministic UVM generator with `DV_REGION` markers; regeneration keeps agent/user regions and never overwrites hand edits. |
| `.../uvm/worklist.py`, `semantic.py` | Lists open regions; the agent implements the smoke-path ones, code splices them in. |
| `.../uvm/lint.py` | Checks generated code against the VLSIT conventions and the rules. |
| `.../uvm/vip.py`, `binding.py`, `vipgen.py` | Probes the VIP, binds its signals to the DUT, generates adapter and bridge. The VIP is never edited. |
| `.../uvm/integrate.py` | RTL phase: reads top/ports/parameters with pyslang, conformance report, DUT wrapper and filelist. |
| `.../uvm/evidence.py` | Records compile/run evidence; PASS/FAIL is read from logs only. |
| `q3tui/flows/dv/tb/rules/vlsit_dut_dv_rule.md` | Rules DUT-DV-001..031. |
| `q3tui/flows/dv/tb/md/uvm_vip.md` | System prompts of the LLM stages. |
| `tests/test_dut_dv_*.py` (7), `tests/dutdv_rtl.py`, `tests/fixtures/mini_axi_vip/` | Automated tests with a fake LLM and a small stand-in RTL/VIP; three VCS tests run only with `Q3TUI_VCS_MODULE` (and `AXI_VIP_HOME`). |
| `docs/spec/dut-dv.md`, `docs/spec/dut-dv-report.md` | Design, milestone status, final report. |
| `examples/axi4_sram_ctrl/` | Worked example: spec, schemas, RTL, generated TB (`src/tb`), reports, tb-only flow `flows/dut_tb.yaml`, README. |

### How to run
```bash
export AXI_VIP_HOME=<axi4_avip checkout @ 86b46171>
module load synopsys/vcs/X-2025.06
cd examples/axi4_sram_ctrl && q3tui run --only tb
Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 AXI_VIP_HOME=... uv run pytest -m vcs     # real-VCS tests
```
Results: `build/sim/tb_uvm/*.log` (logs, `VLSIT_DUT_TB_RESULT` line) and `schemas/dv_evidence.json` (command, return code, UVM_ERROR/FATAL, status).
A run that finds the step up to date does not simulate again.

### Notes and limitations
- 29 of 39 regions are implemented (smoke path, remaining scoreboard rules, TC-002..012); 10 are `DV_TODO` stubs (COV-001..009 and assertions). SBR-006/007/009 are cycle-level pin properties and have no check; TC-007 cannot stimulate backpressure through the VIP adapter. No stress tests.
- SBR-008 is checked only on the SRAM cycles the monitor observes, not on idle cycles. Functional coverage is off.
- `selected_testplan.json` is not produced, so the sva/verify steps are not wired.
- The VIP drops zero-valued data beats, so checking uses a TB-owned pin monitor; VCS-incompatible VIP parts use stand-ins (strong SVA, FCIRS coverage).
- The spec parameters were renamed `PR_*` to `PARA_*` to match the RTL (user decision); no mapping is applied.
- Requires Python 3.12. Live agent runs cost about 4.3 USD (first run); re-running the example from its recorded state costs 0.
- Not committed to `schemas/dv_evidence.json` in the example: it holds machine-specific commands and is written by each run.

---

## Tiếng Việt

### Tóm tắt
Backend tùy chọn cho step `tb` của flow dv. Từ spec đã duyệt, config đã duyệt và một AXI VIP có sẵn, nó dựng testbench UVM, tích hợp RTL đã
release làm DUT, biên dịch bằng VCS và chạy smoke test. Chọn bằng `backend: uvm_vip` trong `q3tui.yaml` (`pipeline.step_options.tb`).
Backend mặc định `plain_sv` không đổi.

Nguyên tắc cốt lõi: spec là hành vi đúng (golden), RTL chỉ là DUT. RTL chỉ được đọc ở pha tích hợp riêng, để lấy cấu trúc (top, port,
parameter, file). Spec và RTL lệch nhau thì báo cáo, không tự điều chỉnh. Hành vi spec chưa nói rõ giữ trạng thái OPEN. Không làm giả kết quả simulator.

### Trạng thái release
| Hạng mục | Trạng thái |
|---|---|
| TC-001 `dut_smoke_test` (VCS X-2025.06, RTL và VIP thật) | **PASS**: 11 checks, 0 mismatches, UVM_ERROR 0, UVM_FATAL 0 |
| TC-002..006, 008, 009, 010, 012 (gồm test 4 KB) | **PASS** (chạy agent thật, khoảng 0.7 USD) |
| TC-011 timing | PASS nhưng **không** kiểm tra timing (SBR-007 không có check) |
| TC-007 backpressure | **INCOMPLETE** (0 check, không phải bằng chứng) |
| Check SBR-006, 007, 009; COV-001..009; assertions | **todo** (chưa kiểm tra / chưa implement) |
| Regression đầy đủ | 349 pass, 4 fail (đã có từ trước: thiếu `examples/sync_fifo/fifo.f`) |

### File đã sửa (4)
| File | Đã sửa gì | Vì sao |
|---|---|---|
| `q3tui/flows/dv/tb/step.py` | Chuyển sang code mới khi `backend == uvm_vip`; kiểm tra input VIP/RTL trước khi chạy; làm mới báo cáo review khi approve. | Cắm backend mới vào step `tb` mà không đụng đường mặc định. |
| `q3tui/steps/vlsit/artifacts.py` | Đăng ký 5 artifact: `golden_manifest`, `verification_contract`, `tb_architecture`, `vip_integration`, `dut_integration`. | Engine cần biết các output mới của step. |
| `pyproject.toml` | Thêm marker pytest `vcs`. | Chọn riêng các test cần VCS thật (`-m vcs`); thiếu VCS thì test tự skip. |
| `.gitignore` | Bỏ ignore cho `examples/axi4_sram_ctrl/.q3tui/pipeline.json`. | Example mang sẵn state của lần chạy đã ghi, nên chạy lại không cần LLM. |

### File thêm mới
| Đường dẫn | Chức năng |
|---|---|
| `q3tui/flows/dv/tb/uvm/__init__.py` | Điều phối các pha và ghi state. |
| `.../uvm/spec_facts.py` | Rút port, assumption và tên gốc từ spec. |
| `.../uvm/contract.py` | Mô hình "hợp đồng kiểm thử", kiểm tra hợp lệ, đóng băng bằng hash. |
| `.../uvm/intent.py`, `task.py` | Stage LLM `tb_intent` điền cột ngữ nghĩa, và prompt của nó. |
| `.../uvm/review.py` | Sổ review: bạn trả lời, nhận default, default tự động, đang chờ. |
| `.../uvm/arch.py` | Kiến trúc testbench. |
| `.../uvm/scaffold.py`, `svgen.py` | Sinh code UVM cố định có vùng `DV_REGION`; sinh lại thì giữ vùng agent/user, không ghi đè phần sửa tay. |
| `.../uvm/worklist.py`, `semantic.py` | Liệt kê vùng còn trống; agent viết các vùng thuộc smoke path, code chèn vào. |
| `.../uvm/lint.py` | Kiểm tra code sinh ra theo quy ước VLSIT và các rule. |
| `.../uvm/vip.py`, `binding.py`, `vipgen.py` | Đọc VIP, nối tín hiệu VIP với DUT, sinh adapter và bridge. Không bao giờ sửa VIP. |
| `.../uvm/integrate.py` | Pha RTL: đọc top/port/parameter bằng pyslang, báo cáo conformance, sinh wrapper và filelist cho DUT. |
| `.../uvm/evidence.py` | Ghi bằng chứng compile/run; PASS/FAIL chỉ đọc từ log. |
| `q3tui/flows/dv/tb/rules/vlsit_dut_dv_rule.md` | Các rule DUT-DV-001..031. |
| `q3tui/flows/dv/tb/md/uvm_vip.md` | System prompt của các stage LLM. |
| `tests/test_dut_dv_*.py` (7), `tests/dutdv_rtl.py`, `tests/fixtures/mini_axi_vip/` | Test tự động dùng LLM giả và RTL/VIP thu nhỏ; 3 test VCS chỉ chạy khi có `Q3TUI_VCS_MODULE` (và `AXI_VIP_HOME`). |
| `docs/spec/dut-dv.md`, `docs/spec/dut-dv-report.md` | Thiết kế, trạng thái milestone, báo cáo cuối. |
| `examples/axi4_sram_ctrl/` | Example hoàn chỉnh: spec, schemas, RTL, TB đã sinh (`src/tb`), báo cáo, flow chỉ có step tb `flows/dut_tb.yaml`, README. |

### Cách chạy
```bash
export AXI_VIP_HOME=<thư mục axi4_avip @ 86b46171>
module load synopsys/vcs/X-2025.06
cd examples/axi4_sram_ctrl && q3tui run --only tb
Q3TUI_VCS_MODULE=synopsys/vcs/X-2025.06 AXI_VIP_HOME=... uv run pytest -m vcs     # test VCS thật
```
Kết quả: `build/sim/tb_uvm/*.log` (log, dòng `VLSIT_DUT_TB_RESULT`) và `schemas/dv_evidence.json` (lệnh, return code, UVM_ERROR/FATAL, trạng thái).
Nếu step đã "up to date" thì không simulate lại.

### Ghi chú và giới hạn
- Đã implement 29/39 vùng (smoke path, các scoreboard rule còn lại, TC-002..012); 10 vùng còn là stub `DV_TODO` (COV-001..009 và assertions). SBR-006/007/009 là thuộc tính mức chu kỳ nên chưa có check; TC-007 không tạo được backpressure qua VIP adapter. Chưa có test stress.
- SBR-008 chỉ kiểm tra trên các chu kỳ SRAM mà monitor quan sát được, không kiểm tra chu kỳ idle. Coverage đang tắt.
- Chưa sinh `selected_testplan.json` nên các step sva/verify chưa được nối.
- VIP làm rơi beat dữ liệu bằng 0 nên việc kiểm tra dùng pin monitor của TB; phần VIP VCS không hỗ trợ dùng stand-in (strong SVA, FCIRS coverage).
- Spec đã đổi tên tham số `PR_*` thành `PARA_*` cho khớp RTL (quyết định của người dùng); không áp dụng mapping.
- Cần Python 3.12. Lần chạy agent thật đầu tốn khoảng 4.3 USD; chạy lại example từ state đã ghi thì 0.
- Example không commit `schemas/dv_evidence.json`: file chứa lệnh riêng của máy và do mỗi lần chạy tự ghi.
