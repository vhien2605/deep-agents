# deep-agents-unittest

*[English](README.md) · Tiếng Việt*

Deep agent sinh unit test cho dự án **C++, Python và Java**, xây trên [`deepagents`](https://docs.langchain.com/labs/deep-agents/overview) (LangChain). Agent tự nhận diện ngôn ngữ của project (Makefile → C++/make/lcov; pyproject.toml/uv.lock → Python/pytest/coverage.py; pom.xml → Java/Maven/JUnit 5/JaCoCo) và dùng bộ skill tương ứng.

Agent chạy vòng lặp: **phân tích mã nguồn (tree-sitter) → sinh test → build & tự sửa lỗi (make) → đo độ phủ (gcov/lcov) → lặp** cho tới khi đạt ngưỡng coverage.

## Kiến trúc

```
main.py (CLI)
└── create_cpp_unittest_agent(project_root)          # src/agent.py
    ├── Model: từ .env — LLM_PROVIDER openai (mọi endpoint OpenAI-compatible) · openrouter · anthropic
    ├── Backend: LocalShellBackend(root=project)      # read/write/edit/glob/grep/execute trên project thật
    ├── Skills (progressive disclosure, skills/*/SKILL.md)
    │   ├── C++:    cpp-analysis · make-build · coverage-lcov
    │   ├── Python: python-analysis · pytest-testing · coverage-py
    │   ├── Java:   java-analysis · maven-junit · coverage-jacoco
    │   ├── Chung:  test-repair — vòng lặp tự sửa test, phân biệt test sai vs bug thật
    │   └── Chung:  symbolic-execution — giải ràng buộc đường đi bằng Z3
    ├── Tools (src/tools/)
    │   ├── analysis.py  — analyze_source_file, get_function_source, list_source_files
    │   │                  (tree-sitter cpp + python + java, đa ngôn ngữ)
    │   ├── build.py     — make_target (C++), run_command (pytest/coverage/mvn...), run_test_binary
    │   ├── coverage.py  — coverage_report (lcov) + coverage_py_report (coverage.py)
    │   │                  + jacoco_report (JaCoCo XML) — cùng một schema JSON
    │   └── symbolic.py  — solve_branch_inputs, extract_path_conditions,
    │                      solve_path_constraints (engine ở src/symbolic.py, Z3)
    └── Subagents (context isolation, mỗi agent một nhiệm vụ)
        ├── code-analyzer    — map codebase, chấm điểm branch_count, read-only
        ├── path-solver      — giải path condition → input cụ thể cho từng nhánh
        ├── test-generator   — viết file test theo convention của project
        ├── test-fixer       — build → đọc lỗi → sửa test → lặp tới khi xanh
        └── coverage-auditor — đo lcov, liệt kê hàm/dòng chưa cover
```

Orchestrator lập kế hoạch bằng todo list (`TodoListMiddleware` của LangChain, được `src/agent.py` đăng ký tường minh — từ deepagents 0.7 nó không còn mặc định), giao việc cho từng subagent, và lặp bước 3–6 nhắm vào phần code chưa cover.

### Symbolic execution trong luồng

Với mọi hàm có `branch_count > 0`, bước SOLVE chạy **trước** khi sinh test:
`solve_branch_inputs` phân tích AST, dựng path condition của từng nhánh, dịch
sang cú pháp Z3 và giải. Kết quả có ba dạng — `sat` (input cụ thể để đi vào
nhánh), `unsat` (nhánh **không thể tới được** → dead code, không viết test), và
`skipped` (có thành phần không dịch được, kèm lý do). Nhờ đó agent không phải
đoán input rồi tốn một vòng generate → build → measure.

Bộ dịch xử lý: `&&`/`||`/`!`, `and`/`or`/`not`, so sánh chuỗi kiểu Python
(`0 < b < 2`), integer promotion của C/Java (`BitVec32` gặp `BitVec64` thì
`SignExt`/`ZeroExt`), so sánh unsigned (`ULT`/`UGT`), hậu tố literal (`3L`,
`10u`), và `len(s)` / `s.length()` / `strlen(s)` → `Length`. Những gì không dịch
được (lời gọi hàm khác, truy cập field, biến local không phải tham số) được
**báo cáo** chứ không đoán bừa.

## Yêu cầu

- `g++`/`clang++`, `make`, `lcov` (macOS: `brew install lcov`)
- File `.env` ở repo root (copy từ `.env.example`). `LLM_PROVIDER` chọn
  `openai` (mọi endpoint OpenAI-compatible), `openrouter` hoặc `anthropic`:
  ```
  LLM_PROVIDER=openai
  LLM_BASE_URL=http://host01:9207/v1
  LLM_API_KEY=sk-...
  LLM_MODEL=zai-org/GLM-5.2
  ```
- `uv sync` để cài dependencies Python

## Chạy

```bash
# Demo: project calculator kèm sẵn (chưa có test nào)
uv run main.py --project examples/demo_calculator

# Project thật, ngưỡng coverage 90%, kèm chỉ dẫn riêng
uv run main.py --project /path/to/cpp/project --coverage 90 \
    --task "Chỉ sinh test cho src/parser.cpp, dùng doctest"
```

Project đích cần một `Makefile` với target `test` (build + chạy test). Target `coverage` (build với `--coverage`, chạy test, xuất `coverage.info`) là tùy chọn — nếu thiếu, agent tự build instrumented và gọi `lcov --capture` (xem `skills/coverage-lcov/SKILL.md`).

## Hướng dẫn dùng extension VS Code (chat với agent)

Thay vì gõ lệnh, bạn chat với agent ngay trong VS Code — giống chat với Claude: nói project nào cần test, agent tự tìm project, sinh test, build, sửa lỗi, đo coverage và báo lại, chữ hiện dần theo kiểu streaming.

### 1. Cài đặt

Cần thêm: **Node.js ≥ 18** và **VS Code ≥ 1.90** (ngoài các yêu cầu ở mục [Yêu cầu](#yêu-cầu)).

```bash
uv sync                                   # dependencies Python (một lần)
cd vscode-extension
npm install
npm run package                           # tạo unittest-agent-0.1.0.vsix
code --install-extension unittest-agent-0.1.0.vsix
```

Sau khi sửa code extension, chạy lại hai lệnh cuối (thêm `--force`) rồi *Developer: Reload Window*.

### 2. Cấu hình

**`.env`** ở repo root — ví dụ dùng Claude API:

```env
LLM_PROVIDER=anthropic
LLM_BASE_URL=
LLM_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-5-5
# Model Claude đời mới từ chối tham số temperature → không gửi
LLM_TEMPERATURE=none
```

**Settings VS Code** (`Ctrl+,` → tìm `unittestAgent`):

| Setting | Giá trị |
|---|---|
| `unittestAgent.repoPath` | Đường dẫn tuyệt đối tới repo này (chỗ có `src/` và `.env`). **Bắt buộc** khi cài từ `.vsix` |
| `unittestAgent.uvPath` | Đường dẫn `uv` nếu VS Code không thấy nó trong PATH (vd. `/home/<user>/.local/bin/uv`) |

### 3. Sử dụng

1. Mở folder chứa project cần test (`File → Open Folder`).
2. Bấm icon **Unit-test Agent** (ngoặc vuông có dấu ✓) ở activity bar bên trái.
3. Gõ yêu cầu bằng tiếng Việt hoặc tiếng Anh rồi Enter, ví dụ:
   - *"viết test cho project này, coverage 90%"*
   - *"test project demo_calculator, chỉ file calculator.cpp"*
   - *"liệt kê các project ở đây"*

Agent tìm project theo tên trong folder đang mở (thư mục có `Makefile` → C++, `pyproject.toml`/`setup.py` → Python, `pom.xml` → Java). Nhiều kết quả thì nó hỏi lại; một kết quả thì chạy luôn. Trong lúc chạy, khung **"Đang chạy…"** hiện từng bước (phân tích, sinh test, sửa lỗi, đo coverage) — bấm vào để xem chi tiết. Xong, agent tóm tắt: file test đã viết, coverage đạt được, bug thật phát hiện được (đánh dấu `// BUG:` trong test).

Agent **ghi file test vào project thật** — nên commit code trước khi chạy để dễ xem diff.

### 4. Lệnh `/`

Gõ `/` để mở menu (↑/↓ chọn, Enter/Tab dùng, Esc đóng):

| Lệnh | Tác dụng |
|---|---|
| `/test [coverage]` | Viết unit test cho project đang mở, vd. `/test 90` (mặc định 80%) |
| `/projects` | Liệt kê các project trong workspace |
| `/new` | Cuộc trò chuyện mới (dừng lượt đang chạy nếu có) |
| `/stop` | Dừng agent — dùng được cả khi đang chạy |
| `/help` | Bảng tất cả lệnh và skill |
| `/<skill> [yêu cầu]` | Gọi một skill trong `skills/`, vd. `/pytest-testing chỉ test utils.py`, `/symbolic-execution`, `/test-repair`. Agent đọc `SKILL.md` của skill đó trước khi làm |

### 5. Lưu ý & xử lý lỗi

- **Dừng** (nút vuông hoặc `/stop`) kill tiến trình agent, nên lịch sử trò chuyện mất theo; tin nhắn sau bắt đầu cuộc mới.
- Log đầy đủ của agent: **Output** panel → chọn *Unit-test Agent*.

| Lỗi hiện trong chat | Cách sửa |
|---|---|
| `Set unittestAgent.repoPath…` | Đặt setting `unittestAgent.repoPath` (mục 2) |
| `Cannot start agent (uv)…` | Đặt `unittestAgent.uvPath` thành đường dẫn tuyệt đối của `uv` |
| `AuthenticationError … invalid x-api-key` | Sai `LLM_API_KEY` trong `.env` |
| `NotFoundError … model: …` | Tên model không có với key này — đổi `LLM_MODEL` |
| `temperature is deprecated for this model` | Đặt `LLM_TEMPERATURE=none` |
| `TOOLCHAIN MISSING` | Cài `make`/`g++`/`lcov` (C++), `mvn` (Java)… cho project đích |

### Cách hoạt động

Extension spawn `uv run python -m src.chat_server` và nói chuyện qua stdio bằng JSON lines (giao thức ghi ở đầu `src/chat_server.py`). Chat agent (`src/chat.py`) có hai tool: `find_projects` và `generate_tests` — tool thứ hai chạy đúng agent của `main.py` trên project được chọn và đẩy tiến độ ra stream. Chỉ model của chat agent bật streaming (`build_chat_model(streaming=True)`); agent sinh test bên trong vẫn tắt streaming như trước. Giao diện nằm ở `vscode-extension/media/` (`chat.css`, `chat.js`, markdown render bằng `marked`).

## Ngân sách bước chạy (`recursion_limit`)

LangGraph đếm mỗi lần một node chạy là một super-step, và một lượt của agent tốn ba step (`model` → `TodoListMiddleware.after_model` → `tools`), nên giới hạn N chỉ cho phép khoảng **N/3 tool call**. Mặc định 200 của LangGraph tương đương ~66 tool call — vòng lặp analyze → solve → generate → fix → measure tiêu hết số đó trên bất kỳ project nào không tầm thường, và run chết với `GraphRecursionError`.

Vì vậy mặc định ở đây là **600** (`config.DEFAULT_RECURSION_LIMIT`), ghi đè từng lần chạy bằng `--recursion-limit`, ghi đè toàn cục bằng `AGENT_RECURSION_LIMIT` trong `.env`. Mỗi subagent thừa hưởng đúng ngân sách này cho run của riêng nó — và vì `GraphRecursionError` phát sinh trong subagent sẽ bung ra khỏi tool `task`, một subagent chạy loạn đủ để giết cả phiên.

Tăng ngân sách không thay thế được việc biết dừng: prompt của orchestrator, test-fixer và coverage-auditor đều có điều kiện dừng tường minh (thiếu toolchain, không đo được coverage, hai vòng liên tiếp không tăng coverage, giới hạn số vòng lặp), còn khi thiếu `make`/`g++`/`lcov`/`mvn` thì tool báo `TOOLCHAIN MISSING` — lỗi cuối, agent được dặn không bao giờ thử lại.

## Benchmark (`bench/`)

`bench/` đo agent theo cách bài báo CITYWALK đo các bộ sinh unit test C++: mỗi focal method chạy
agent một lần, rồi compile / chạy / đo coverage kết quả và báo cáo theo từng method CSR, EPR,
line/branch coverage và mutation score. Benchmark chạy trong Docker để cố định toolchain
(g++, gcovr, GoogleTest):

```bash
docker build -t deepagents-bench -f bench/Dockerfile .
bench/run-docker.sh run --project data/re2-<sha> --out runs/re2 --sample 30
```

Xem [`bench/README.md`](bench/README.md) cho các lệnh `discover` / `gen` / `eval` / `report` và
ý nghĩa các cột kết quả.

## Test

Bộ test chạy offline — không cần LLM, không cần API key:

```bash
uv run --with pytest pytest -q
```

## Quy tắc an toàn đã cài vào prompt

- Không bao giờ sửa production source để test pass — chỉ sửa test.
- Test lộ ra bug thật thì giữ nguyên assertion đúng, đánh dấu `// BUG:` và báo cáo.
- Cấm "sửa" bằng cách xóa assertion hoặc nới expectation cho luôn pass.

Lưu ý: `LocalShellBackend` cho agent chạy shell thật trên máy — chỉ dùng cho project local tin cậy, hoặc bọc trong sandbox/container khi chạy code lạ.
