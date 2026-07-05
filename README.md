# Kakao Reply Assistant

Windows PC에서 열린 카카오톡 채팅창을 읽고, 지정한 대상자의 최신 메시지에 대한 답장 초안을 OpenAI API로 생성하는 로컬 도우미입니다.

자동 전송은 하지 않습니다. 프로그램은 작은 로컬 팝업에 답장 초안을 보여주고, 사용자가 `복사` 버튼을 눌러 직접 카카오톡에 붙여넣어 전송합니다.

## 현재 상태

현재 안정적으로 동작하는 경로는 **열려 있는 카카오톡 채팅창 캡처 + OpenAI 이미지 판독** 방식입니다.

초기에는 Windows 알림 DB, UserNotificationListener, 카카오톡 자체 팝업 감지를 시도했지만, 현재 테스트 PC에서는 카카오톡 메시지 본문이 Windows 알림/접근성 API에 안정적으로 노출되지 않았습니다. 최종적으로 `dxcam` 기반 DXGI 캡처를 사용해 카카오톡 채팅창 이미지를 읽는 방식이 성공했습니다.

## 주요 기능

- 대상자 이름 기반 필터링: `target_senders`
- 열린 카카오톡 채팅창 제목 매칭
- `dxcam` 기반 채팅창 캡처
- OpenAI 이미지 입력으로 채팅창 판독
- 내 마지막 노란/오른쪽 말풍선 이후 상대 메시지 기준 답장 생성
- 최근 5분 내 같은 채팅방/발신자/메시지 중복 팝업 방지
- Tkinter 로컬 팝업 표시
- `복사` 버튼으로 클립보드 저장
- `새 답변` 버튼으로 같은 메시지에 대한 초안 재생성
- 생성 기록 저장: `data/messages.jsonl`
- 처리 상태 저장: `data/state.json`
- 진단 명령 제공: `--watch-chat`, `--list-chat-windows`, `--dump-chat-ui`, `--show-last-draft`

## 설치

Python 3.9 이상을 사용합니다.

채팅창 캡처를 위해 `dxcam`과 `Pillow`가 필요합니다.

```powershell
python -m pip install dxcam pillow
```

OpenAI API 키는 환경 변수로 설정하는 방식을 권장합니다.

```powershell
setx OPENAI_API_KEY "YOUR_OPENAI_API_KEY"
```

`setx` 후에는 새 PowerShell을 열어 실행하세요.

## 설정

기본 설정 파일을 생성합니다.

```powershell
python kakao_reply_assistant.py --init-config
```

생성된 `config.json`에서 대상자를 수정합니다.

```json
{
  "target_senders": ["김민수", "박지현"],
  "sender_match_mode": "contains",
  "kakao_chat_window_enabled": true,
  "kakao_chat_capture_method": "auto"
}
```

중요 설정:

- `target_senders`: 답장 초안을 만들 대상 이름 목록
- `sender_match_mode`: `contains` 또는 `exact`
- `kakao_chat_window_enabled`: 열린 채팅창 기반 모드 사용 여부
- `kakao_chat_capture_method`: 기본값 `auto`; `dxcam`, `screen`, `window` 등 선택 가능
- `kakao_chat_dedup_seconds`: 같은 채팅방/발신자/메시지 중복 방지 시간. 기본값 300초
- `reply_style`: 답장 톤
- `save_messages`: 생성된 메시지/초안 로그 저장 여부

## 실행

대상자와의 카카오톡 채팅창을 화면에 보이게 열어둔 뒤 실행합니다.

```powershell
python kakao_reply_assistant.py
```

프로그램이 채팅창을 감시하다가 답장 대상 메시지를 찾으면 팝업을 띄웁니다.

## 테스트와 진단

채팅창 후보를 확인합니다.

```powershell
python kakao_reply_assistant.py --list-chat-windows
```

채팅창 캡처와 OpenAI 이미지 판독을 테스트합니다.

```powershell
python kakao_reply_assistant.py --watch-chat 60
```

카카오톡 UI Automation 텍스트 노출 여부를 확인합니다.

```powershell
python kakao_reply_assistant.py --dump-chat-ui
```

마지막으로 저장된 답장 초안 팝업을 다시 띄웁니다.

```powershell
python kakao_reply_assistant.py --show-last-draft
```

팝업 UI만 테스트합니다.

```powershell
python kakao_reply_assistant.py --test-popup
```

## 동작 원리

1. KakaoTalk.exe 창 목록을 확인합니다.
2. 창 제목이 `target_senders`와 매칭되는지 확인합니다.
3. 매칭된 창을 `dxcam` 등으로 캡처합니다.
4. 캡처 이미지를 OpenAI 이미지 입력으로 보냅니다.
5. AI가 채팅창에서 상대 메시지와 내 메시지를 구분합니다.
6. 내 마지막 노란/오른쪽 말풍선 아래에 있는 상대 메시지를 답장 대상으로 판단합니다.
7. 답장 초안 1개를 생성합니다.
8. 로컬 팝업에 표시합니다.
9. 사용자가 직접 복사해서 카카오톡에 전송합니다.

## 보안과 개인정보 주의

이 프로그램은 카카오톡 화면 캡처 이미지와 메시지 내용을 OpenAI API로 전송할 수 있습니다. 화면에 보이는 이전 대화, 이미지, 프로필, 민감정보가 함께 포함될 수 있으므로 대상자와 캡처 범위를 좁게 유지하세요.

`data/` 폴더에는 테스트 캡처, 메시지 로그, 처리 상태가 저장될 수 있습니다. 이 저장소에서는 `data/`와 `config.json`이 `.gitignore`로 제외되어 있지만, 로컬 PC나 OneDrive에는 남을 수 있습니다.

민감한 대화에서는 사용하지 않거나 다음 설정을 고려하세요.

```json
{
  "save_messages": false,
  "save_chat_captures": false,
  "save_popup_captures": false
}
```

## 제한 사항

- 현재 구조는 개인톡 중심입니다.
- 단체톡에서 특정 발신자만 골라 답장하는 `target_rooms` 기능은 아직 없습니다.
- 카카오톡 창이 최소화되거나 가려져 있으면 캡처가 불안정할 수 있습니다.
- AI 이미지 판독이므로 최신 메시지 판단이 항상 완벽하지는 않습니다.
- 카카오톡이나 Windows 알림 구조가 바뀌면 감지 방식이 깨질 수 있습니다.

## 개발 노트

상세한 개발 과정과 시행착오는 [DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md)에 정리되어 있습니다.
