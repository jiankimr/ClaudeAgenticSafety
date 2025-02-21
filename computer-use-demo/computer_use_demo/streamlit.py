"""
Entrypoint for streamlit, see https://docs.streamlit.io/
"""
import io
import asyncio
import base64
import os
import sys
import subprocess
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import PosixPath
from typing import cast
import json
from datetime import datetime
import streamlit.components.v1 as components

import httpx
import streamlit as st
from anthropic import RateLimitError
from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaTextBlockParam,
    BetaToolResultBlockParam,
)
from streamlit.delta_generator import DeltaGenerator

from computer_use_demo.loop import (
    PROVIDER_TO_DEFAULT_MODEL_NAME,
    APIProvider,
    sampling_loop,
)
from computer_use_demo.tools import ToolResult

CONFIG_DIR = PosixPath("~/.anthropic").expanduser()
API_KEY_FILE = CONFIG_DIR / "api_key"
STREAMLIT_STYLE = """
<style>
    /* Highlight the stop button in red */
    button[kind=header] {
        background-color: rgb(255, 75, 75);
        border: 1px solid rgb(255, 75, 75);
        color: rgb(255, 255, 255);
    }
    button[kind=header]:hover {
        background-color: rgb(255, 51, 51);
    }
     /* Hide the streamlit deploy button */
    .stAppDeployButton {
        visibility: hidden;
    }
</style>
"""

WARNING_TEXT = "⚠️ Security Alert: Never provide access to sensitive accounts or data, as malicious web content can hijack Claude's behavior"
INTERRUPT_TEXT = "(user stopped or interrupted and wrote the following)"
INTERRUPT_TOOL_ERROR = "human stopped or interrupted tool execution"

json_path = "/home/computeruse/computer_use_demo/HarmGUI.json"
LAST_TASK_FILE = "/home/computeruse/computer_use_demo/last_task.json"

def load_last_task():
    """마지막 실행한 identifier를 불러오는 함수"""
    if os.path.exists(LAST_TASK_FILE):
        try:
            with open(LAST_TASK_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
                return data.get("last_identifier")
        except json.JSONDecodeError:
            st.warning("⚠️ 마지막 실행 기록 파일이 손상되었습니다. 처음부터 실행합니다.")
            return None
    return None

def save_last_task(identifier):
    """현재 실행 중인 identifier를 저장하는 함수"""
    try:
        with open(LAST_TASK_FILE, "w", encoding="utf-8") as file:
            json.dump({"last_identifier": identifier}, file, indent=4, ensure_ascii=False)
    except Exception as e:
        st.error(f"❌ 마지막 실행 기록 저장 실패: {e}")

def load_json_from_path(file_path):
    absolute_path = os.path.abspath(file_path)  

    if not os.path.exists(absolute_path):
        st.error(f"⚠️ JSON 파일이 존재하지 않습니다: {absolute_path}")
        return []

    try:
        with open(absolute_path, "r", encoding="utf-8") as file:
            data = json.load(file)
             # "task" 값만 추출하여 리스트로 반환
            return [item["task"] for item in data if "task" in item]
    
            st.success(f"✅ JSON 파일 로드 성공: {absolute_path}")
            return data
        
    except Exception as e:
        st.error(f"❌ JSON 파일 로드 중 오류 발생: {e}")
        return []

def load_tasks_from_json(file_path):
    """JSON 파일에서 identifier와 task를 함께 로드"""
    if not os.path.exists(file_path):
        st.error(f"⚠️ JSON 파일이 존재하지 않습니다: {file_path}")
        return []
    
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        
        # 데이터 형식 검사 및 변환
        if not isinstance(data, list):  # JSON 파일이 리스트 형태가 아닌 경우
            st.error("❌ JSON 파일의 형식이 잘못되었습니다. 리스트여야 합니다.")
            return []

        formatted_data = []
        for item in data:
            if isinstance(item, dict) and "identifier" in item and "task" in item:
                formatted_data.append({"identifier": item["identifier"], "task": item["task"]})
            else:
                st.warning(f"⚠️ JSON 항목이 올바른 형식이 아닙니다: {item}")

        return formatted_data

    except json.JSONDecodeError as e:
        st.error(f"❌ JSON 파일 로드 중 오류 발생 (잘못된 형식): {e}")
        return []
    except Exception as e:
        st.error(f"❌ JSON 파일 로드 중 예기치 않은 오류 발생: {e}")
        return []

def get_next_task():
    """다음 task를 identifier와 함께 가져오는 함수"""
    if "tasks" not in st.session_state:
        st.session_state.tasks = load_tasks_from_json(json_path)

    if "task_index" not in st.session_state:
        st.session_state.task_index = 0

    last_identifier = load_last_task()  # 마지막 실행한 identifier 불러오기

    # ✅ 마지막 실행된 identifier 이후의 task부터 실행
    if last_identifier:
        found = False
        for idx, task in enumerate(st.session_state.tasks):
            if task["identifier"] == last_identifier:
                st.session_state.task_index = idx + 1  # 마지막 identifier 이후의 task부터 실행
                st.success(f"🔄 이전 실행된 task({last_identifier})를 확인했습니다. 이어서 실행합니다.")
                found = True
                break
        if not found:
            st.warning(f"⚠️ 저장된 identifier({last_identifier})가 목록에 없습니다. 처음부터 실행합니다.")
            st.session_state.task_index = 0  # identifier가 목록에 없으면 처음부터 실행

    # ✅ 다음 task 가져오기
    if st.session_state.task_index < len(st.session_state.tasks):
        next_task_data = st.session_state.tasks[st.session_state.task_index]
        st.session_state.task_index += 1

        if isinstance(next_task_data, dict) and "identifier" in next_task_data and "task" in next_task_data:
            save_last_task(next_task_data["identifier"])  # ✅ 실행 직전 identifier 저장
            return next_task_data["identifier"], next_task_data["task"]
        else:
            st.error(f"❌ 잘못된 Task 데이터: {next_task_data}")
            return None, None
    else:
        return None, None  # 모든 task 완료됨


class Sender(StrEnum):
    USER = "user"
    BOT = "assistant"
    TOOL = "tool"


def setup_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "api_key" not in st.session_state:
        # Try to load API key from file first, then environment
        st.session_state.api_key = load_from_storage("api_key") or os.getenv(
            "ANTHROPIC_API_KEY", ""
        )
    if "provider" not in st.session_state:
        st.session_state.provider = (
            os.getenv("API_PROVIDER", "anthropic") or APIProvider.ANTHROPIC
        )
    if "provider_radio" not in st.session_state:
        st.session_state.provider_radio = st.session_state.provider
    if "model" not in st.session_state:
        _reset_model()
    if "auth_validated" not in st.session_state:
        st.session_state.auth_validated = False
    if "responses" not in st.session_state:
        st.session_state.responses = {}
    if "tools" not in st.session_state:
        st.session_state.tools = {}
    if "only_n_most_recent_images" not in st.session_state:
        st.session_state.only_n_most_recent_images = 3
    if "custom_system_prompt" not in st.session_state:
        st.session_state.custom_system_prompt = load_from_storage("system_prompt") or ""
    if "hide_images" not in st.session_state:
        st.session_state.hide_images = False
    if "in_sampling_loop" not in st.session_state:
        st.session_state.in_sampling_loop = False
    if "log_saved" not in st.session_state:
        st.session_state.log_saved = False
    if "download_ready" not in st.session_state:
        st.session_state.download_ready = False
    if "saved_file_name" not in st.session_state:
        st.session_state.saved_file_name = ""
    if "saved_file_content" not in st.session_state:
        st.session_state.saved_file_content = None  # 메모리 저장 방식으로 변경
    if "last_message_count" not in st.session_state:
        st.session_state.last_message_count = 0
    # JSON에서 task 로드
    if "tasks" not in st.session_state:
        st.session_state.tasks = load_tasks_from_json(json_path)  # JSON에서 task 로드
    # 현재 사용 중인 Task 인덱스 확인
    if "task_index" not in st.session_state:
        st.session_state.task_index = 0

def _reset_model():
    st.session_state.model = PROVIDER_TO_DEFAULT_MODEL_NAME[
        cast(APIProvider, st.session_state.provider)
    ]


async def main():
    """Render loop for streamlit"""
    setup_state()

    st.markdown(STREAMLIT_STYLE, unsafe_allow_html=True)

    st.title("Claude Computer Use Demo")

    # st.write(f"📌 log_saved status: {st.session_state.log_saved}")
    # st.write(f"📌 download_ready status: {st.session_state.download_ready}")
    # st.write(f"📌 in_sampling_loop status: {st.session_state.in_sampling_loop}")
    # st.write("📥 current message status:", st.session_state.messages)
    # st.write("📂 JSON 파일 경로:", json_path)
    # st.write("🔍 JSON 절대 파일 경로:", os.path.abspath(json_path))
    # 현재 실행 중인 작업 디렉토리 확인
    current_dir = os.getcwd()
    # st.write(f"📂 현재 작업 디렉토리: {current_dir}")
    # st.write(f"📂 현재 Streamlit 작업 디렉토리: `{os.getcwd()}`")
    # st.write(f"🐍 실행 중인 Python 경로: `{sys.executable}`")
    # st.write(f"📦 사용 중인 Python 환경: `{sys.version}`")

    # if os.path.exists(json_path):
    #     st.success(f"✅ JSON 파일이 존재합니다: `{json_path}`")
    # else:
    #     st.error(f"⚠️ JSON 파일이 존재하지 않습니다: `{json_path}`")

    # JSON 파일 로드 시도
    json_data = load_json_from_path(json_path)
    # 불러온 데이터 확인
    st.write("📄 불러온 JSON 데이터:", json_data)
    # st.write("🔄 불러온 Task 목록:", st.session_state.tasks)  # 전체 task 리스트 확인
    # st.write(f"📌 현재 Task Index: {st.session_state.task_index}")
    #st.write(f"🎯 현재 할당된 Task: {new_task}")


    if not os.getenv("HIDE_WARNING", False):
        st.warning(WARNING_TEXT)

    with st.sidebar:

        def _reset_api_provider():
            if st.session_state.provider_radio != st.session_state.provider:
                _reset_model()
                st.session_state.provider = st.session_state.provider_radio
                st.session_state.auth_validated = False

        provider_options = [option.value for option in APIProvider]
        st.radio(
            "API Provider",
            options=provider_options,
            key="provider_radio",
            format_func=lambda x: x.title(),
            on_change=_reset_api_provider,
        )

        st.text_input("Model", key="model")

        if st.session_state.provider == APIProvider.ANTHROPIC:
            st.text_input(
                "Anthropic API Key",
                type="password",
                key="api_key",
                on_change=lambda: save_to_storage("api_key", st.session_state.api_key),
            )

        st.number_input(
            "Only send N most recent images",
            min_value=0,
            key="only_n_most_recent_images",
            help="To decrease the total tokens sent, remove older screenshots from the conversation",
        )
        st.text_area(
            "Custom System Prompt Suffix",
            key="custom_system_prompt",
            help="Additional instructions to append to the system prompt. see computer_use_demo/loop.py for the base system prompt.",
            on_change=lambda: save_to_storage(
                "system_prompt", st.session_state.custom_system_prompt
            ),
        )
        st.checkbox("Hide screenshots", key="hide_images")

        if st.button("Reset", type="primary"):
            with st.spinner("Resetting..."):
                st.session_state.clear()
                setup_state()

                subprocess.run("pkill Xvfb; pkill tint2", shell=True)  # noqa: ASYNC221
                await asyncio.sleep(1)
                subprocess.run("./start_all.sh", shell=True)  # noqa: ASYNC221

    if not st.session_state.auth_validated:
        if auth_error := validate_auth(
            st.session_state.provider, st.session_state.api_key
        ):
            st.warning(f"Please resolve the following auth issue:\n\n{auth_error}")
            return
        else:
            st.session_state.auth_validated = True

    chat, http_logs = st.tabs(["Chat", "HTTP Exchange Logs"])

    #automatic input
    if st.button("start automatic attack!"):
        await run_task_loop(http_logs)

    #new_message=get_next_task()
    new_message = st.chat_input(
        "Type a message to send to Claude to control the computer..."
    )
    with chat:
        # render past chats
        for message in st.session_state.messages:
            if isinstance(message["content"], str):
                _render_message(message["role"], message["content"])
            elif isinstance(message["content"], list):
                for block in message["content"]:
                    # the tool result we send back to the Anthropic API isn't sufficient to render all details,
                    # so we store the tool use responses
                    if isinstance(block, dict) and block["type"] == "tool_result":
                        _render_message(
                            Sender.TOOL, st.session_state.tools[block["tool_use_id"]]
                        )
                    else:
                        _render_message(
                            message["role"],
                            cast(BetaContentBlockParam | ToolResult, block),
                        )

        # render past http exchanges
        for identity, (request, response) in st.session_state.responses.items():
            _render_api_response(request, response, identity, http_logs)

        # render past chats
        if new_message:
            st.session_state.messages.append(
                {
                    "role": Sender.USER,
                    "content": [
                        *maybe_add_interruption_blocks(),
                        BetaTextBlockParam(type="text", text=new_message),
                    ],
                }
            )
            _render_message(Sender.USER, new_message)

        try:
            most_recent_message = st.session_state["messages"][-1]
        except IndexError:
            return

        if most_recent_message["role"] is not Sender.USER:
            # we don't have a user message to respond to, exit early
            return

        with track_sampling_loop():
            # run the agent sampling loop with the newest message
            st.session_state.messages = await sampling_loop(
                system_prompt_suffix=st.session_state.custom_system_prompt,
                model=st.session_state.model,
                provider=st.session_state.provider,
                messages=st.session_state.messages,
                output_callback=partial(_render_message, Sender.BOT),
                tool_output_callback=partial(
                    _tool_output_callback, tool_state=st.session_state.tools
                ),
                api_response_callback=partial(
                    _api_response_callback,
                    tab=http_logs,
                    response_state=st.session_state.responses,
                ),
                api_key=st.session_state.api_key,
                only_n_most_recent_images=st.session_state.only_n_most_recent_images,
            )

def maybe_add_interruption_blocks():
    if not st.session_state.in_sampling_loop:
        return []
    # If this function is called while we're in the sampling loop, we can assume that the previous sampling loop was interrupted
    # and we should annotate the conversation with additional context for the model and heal any incomplete tool use calls
    result = []
    last_message = st.session_state.messages[-1]
    previous_tool_use_ids = [
        block["id"] for block in last_message["content"] if block["type"] == "tool_use"
    ]
    for tool_use_id in previous_tool_use_ids:
        st.session_state.tools[tool_use_id] = ToolResult(error=INTERRUPT_TOOL_ERROR)
        result.append(
            BetaToolResultBlockParam(
                tool_use_id=tool_use_id,
                type="tool_result",
                content=INTERRUPT_TOOL_ERROR,
                is_error=True,
            )
        )
    result.append(BetaTextBlockParam(type="text", text=INTERRUPT_TEXT))
    return result


@contextmanager
def track_sampling_loop():
    st.session_state.in_sampling_loop = True
    yield
    st.session_state.in_sampling_loop = False


def validate_auth(provider: APIProvider, api_key: str | None):
    if provider == APIProvider.ANTHROPIC:
        if not api_key:
            return "Enter your Anthropic API key in the sidebar to continue."
    if provider == APIProvider.BEDROCK:
        import boto3

        if not boto3.Session().get_credentials():
            return "You must have AWS credentials set up to use the Bedrock API."
    if provider == APIProvider.VERTEX:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        if not os.environ.get("CLOUD_ML_REGION"):
            return "Set the CLOUD_ML_REGION environment variable to use the Vertex API."
        try:
            google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        except DefaultCredentialsError:
            return "Your google cloud credentials are not set up correctly."


def load_from_storage(filename: str) -> str | None:
    """Load data from a file in the storage directory."""
    try:
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            data = file_path.read_text().strip()
            if data:
                return data
    except Exception as e:
        st.write(f"Debug: Error loading {filename}: {e}")
    return None


def save_to_storage(filename: str, data: str) -> None:
    """Save data to a file in the storage directory."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = CONFIG_DIR / filename
        file_path.write_text(data)
        # Ensure only user can read/write the file
        file_path.chmod(0o600)
    except Exception as e:
        st.write(f"Debug: Error saving {filename}: {e}")


def _api_response_callback(
    request: httpx.Request,
    response: httpx.Response | object | None,
    error: Exception | None,
    tab: DeltaGenerator,
    response_state: dict[str, tuple[httpx.Request, httpx.Response | object | None]],
):
    """
    Handle an API response by storing it to state and rendering it.
    """
    response_id = datetime.now().isoformat()
    response_state[response_id] = (request, response)
    if error:
        _render_error(error)
    _render_api_response(request, response, response_id, tab)


def _tool_output_callback(
    tool_output: ToolResult, tool_id: str, tool_state: dict[str, ToolResult]
):
    """Handle a tool output by storing it to state and rendering it."""
    tool_state[tool_id] = tool_output
    _render_message(Sender.TOOL, tool_output)


def _render_api_response(
    request: httpx.Request,
    response: httpx.Response | object | None,
    response_id: str,
    tab: DeltaGenerator,
):
    """Render an API response to a streamlit tab"""
    with tab:
        with st.expander(f"Request/Response ({response_id})"):
            newline = "\n\n"
            st.markdown(
                f"`{request.method} {request.url}`{newline}{newline.join(f'`{k}: {v}`' for k, v in request.headers.items())}"
            )
            st.json(request.read().decode())
            st.markdown("---")
            if isinstance(response, httpx.Response):
                st.markdown(
                    f"`{response.status_code}`{newline}{newline.join(f'`{k}: {v}`' for k, v in response.headers.items())}"
                )
                st.json(response.text)
            else:
                st.write(response)


def _render_error(error: Exception):
    if isinstance(error, RateLimitError):
        body = "You have been rate limited."
        if retry_after := error.response.headers.get("retry-after"):
            body += f" **Retry after {str(timedelta(seconds=int(retry_after)))} (HH:MM:SS).** See our API [documentation](https://docs.anthropic.com/en/api/rate-limits) for more details."
        body += f"\n\n{error.message}"
    else:
        body = str(error)
        body += "\n\n**Traceback:**"
        lines = "\n".join(traceback.format_exception(error))
        body += f"\n\n```{lines}```"
    save_to_storage(f"error_{datetime.now().timestamp()}.md", body)
    st.error(f"**{error.__class__.__name__}**\n\n{body}", icon=":material/error:")


def _render_message(
    sender: Sender,
    message: str | BetaContentBlockParam | ToolResult,
):
    """Convert input from the user or output from the agent to a streamlit message."""
    # streamlit's hotreloading breaks isinstance checks, so we need to check for class names
    is_tool_result = not isinstance(message, str | dict)
    if not message or (
        is_tool_result
        and st.session_state.hide_images
        and not hasattr(message, "error")
        and not hasattr(message, "output")
    ):
        return
    with st.chat_message(sender):
        if is_tool_result:
            message = cast(ToolResult, message)
            if message.output:
                if message.__class__.__name__ == "CLIResult":
                    st.code(message.output)
                else:
                    st.markdown(message.output)
            if message.error:
                st.error(message.error)
            if message.base64_image and not st.session_state.hide_images:
                st.image(base64.b64decode(message.base64_image))
        elif isinstance(message, dict):
            if message["type"] == "text":
                st.write(message["text"])
            elif message["type"] == "tool_use":
                st.code(f'Tool Use: {message["name"]}\nInput: {message["input"]}')
            else:
                # only expected return types are text and tool_use
                raise Exception(f'Unexpected response type {message["type"]}')
        else:
            st.markdown(message)

def download_chat_logs():
    if not st.session_state.messages:
        st.write("⚠️ No messages to save")
        return None

    if st.session_state.log_saved:
        st.write("⚠️ Log has already been saved")
        return None
    
    st.session_state.log_saved = True
     # 가장 최근 identifier 가져오기 (없으면 "unknown")
    last_identifier = st.session_state.get("current_identifier", "unknown")

    # 날짜만 포함된 timestamp 생성
    timestamp = datetime.now().strftime("%Y-%m-%d")

    processed_messages = []

    for msg in st.session_state.messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        # user 역할이지만 tool_result 타입을 포함한 경우 role을 assistant로 변경
        if role == "user" and isinstance(content, list):
            for item in content:
                if item.get("type") == "tool_result":
                    role = "assistant"
                    break  # 한 개만 있어도 변경하므로 빠르게 종료

        processed_messages.append({"role": role, "content": content})

    log_data = {
        "timestamp": timestamp,
        "identifier": last_identifier,  # identifier 추가
        "messages": processed_messages,
    }
    json_bytes = json.dumps(log_data, indent=4, ensure_ascii=False).encode("utf-8")
    st.session_state.saved_file_content = io.BytesIO(json_bytes)
    st.session_state.saved_file_name = f"chat_log_{timestamp}_{last_identifier}.json"
    st.write("✅ Log saved completed:", st.session_state.saved_file_name)
    st.write("📄 Stored data length:", len(json_bytes))
    return True


def trigger_auto_download():
    """automatic download trigger"""
    if not st.session_state.saved_file_content:
        st.write("⚠️ No messages to save")
        return
    
    # Base64 데이터 생성
    st.session_state.saved_file_content.seek(0)
    file_data = st.session_state.saved_file_content.read()
    b64_data = base64.b64encode(file_data).decode()
    file_name = st.session_state.saved_file_name

    # JavaScript HTML 생성
    js_code = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="X-UA-Compatible" content="IE=edge">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Auto Download</title>
    </head>
    <body>
        <script>
            // Base64 데이터를 Blob으로 변환
            const b64Data = "{b64_data}";
            const byteCharacters = atob(b64Data);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {{
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }}
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], {{ type: "application/json" }});

            // Blob URL 생성 및 다운로드
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = "{file_name}";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            console.log("✅ Log saved completed");
        </script>
    </body>
    </html>
    """
    components.html(js_code, height=0)
    st.write("🚀 Automatic download trigger execution complete!")

@contextmanager
def track_sampling_loop():
    """State management during sampling loop progress"""
    st.session_state.in_sampling_loop = True
    st.write("🔄 Start sampling loop")
    yield
    st.session_state.in_sampling_loop = False
    st.write("✅ End sampling loop")

    # 대화 로그 저장
    success = download_chat_logs()
    last_identifier = st.session_state.get("current_identifier", "unknown")
    if success and not last_identifier.startswith("scenchg"):
        st.session_state.download_ready = True
        st.write("📂 Conversation auto-save completed!")
        trigger_auto_download()
    
    """"""
    # Reset message after saving log (start new conversation)
    if st.session_state.messages:
        #last_message = st.session_state.messages[-1]  # Save last user input
        #st.session_state.messages = [last_message]  # Reset leaving only the last message
        st.session_state.messages=[]

    #After saving the log, initialize the state (prepare to receive the next input)
    st.session_state.log_saved = False

async def run_task_loop(http_logs):
    """Task를 반복해서 실행하는 루프 (중단된 위치부터 재시작)"""
    while True:
        new_identifier, new_task = get_next_task()

        if new_task is None:
            st.warning("All tasks are exhausted. End.")
            break  # 모든 Task가 끝났으면 종료

        st.session_state.current_identifier = new_identifier  

        # ✅ 실행 직전에 identifier 저장
        save_last_task(new_identifier)

        # 새로운 Task를 Messages에 추가
        st.session_state.messages.append(
            {
                "role": Sender.USER,
                "content": [
                    *maybe_add_interruption_blocks(),
                    BetaTextBlockParam(type="text", text=new_task),
                ],
            }
        )
        _render_message(Sender.USER, new_task)
        st.success(f"New Task assigned: [{new_identifier}] {new_task}")

        # 🚀 새로운 Task를 Claude가 자동으로 실행하도록 다시 샘플링 루프 실행
        with track_sampling_loop():
            st.session_state.messages = await sampling_loop(
                system_prompt_suffix=st.session_state.custom_system_prompt,
                model=st.session_state.model,
                provider=st.session_state.provider,
                messages=st.session_state.messages,
                output_callback=partial(_render_message, Sender.BOT),
                tool_output_callback=partial(
                    _tool_output_callback, tool_state=st.session_state.tools
                ),
                api_response_callback=partial(
                    _api_response_callback,
                    tab=http_logs,
                    response_state=st.session_state.responses,
                ),
                api_key=st.session_state.api_key,
                only_n_most_recent_images=st.session_state.only_n_most_recent_images,
            )

        await asyncio.sleep(4)  # 너무 빠른 반복을 방지하기 위해 4초 대기


if __name__ == "__main__":
    asyncio.run(main())