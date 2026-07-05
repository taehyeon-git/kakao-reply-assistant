# Development Notes

## 1. Project Goal

The goal was to build a local Windows assistant that helps draft KakaoTalk replies without automatically sending messages.

The intended user flow is:

1. A KakaoTalk message arrives.
2. The program detects whether it is from a configured target sender.
3. The message is sent to OpenAI for a reply draft.
4. A small local popup shows the generated draft.
5. The user copies the draft and manually sends it in KakaoTalk.

Automatic sending was intentionally excluded so the user keeps final control.

## 2. Current MVP

The working MVP is based on visible KakaoTalk chat window capture.

Current working flow:

1. The target KakaoTalk chat window is open and visible.
2. The program finds KakaoTalk windows.
3. The window title is matched against `target_senders`.
4. The chat window is captured with `dxcam`.
5. The capture is sent to OpenAI image input.
6. OpenAI identifies the latest target message.
7. A reply draft is generated.
8. A local Tkinter popup displays the draft.
9. The user copies and manually sends it.

## 3. Implemented Features

- JSON config file generation with `--init-config`
- Target sender filtering through `target_senders`
- Sender matching with `contains` or `exact`
- Windows notification database reader
- Windows UserNotificationListener diagnostics
- KakaoTalk popup/window diagnostics
- KakaoTalk chat window detection
- DPI-aware window coordinate handling
- `dxcam`-based chat window capture
- OpenAI Responses API integration
- OpenAI image input for chat window reading
- Reply style configuration through `reply_style`
- Tkinter popup with copy button
- Popup `새 답변` button for regenerating a draft
- Recent duplicate suppression by room/sender/message
- Message and draft logging to `data/messages.jsonl`
- Runtime state tracking in `data/state.json`
- Test command for last draft popup: `--show-last-draft`
- Unit tests for notification parsing, OpenAI response parsing, and chat deduplication

## 4. Main Implementation Files

- `kakao_reply_assistant.py`: main application
- `config.example.json`: example config
- `README.md`: user guide
- `tests/test_kakao_reply_assistant.py`: unit tests
- `.gitignore`: excludes local secrets and private runtime data

Ignored local files:

- `config.json`
- `data/`
- `__pycache__/`
- `.pytest_cache/`

## 5. Key Config Options

```json
{
  "target_senders": ["홍길동"],
  "sender_match_mode": "contains",
  "kakao_chat_window_enabled": true,
  "kakao_chat_capture_method": "auto",
  "kakao_chat_dedup_seconds": 300,
  "reply_style": "친근하고 자연스럽게, 너무 길지 않게"
}
```

## 6. Development History

### Attempt 1: Windows notification database

The first implementation read Windows notification storage and parsed toast XML payloads.

This worked for some apps, but KakaoTalk messages did not reliably appear in the Windows notification database on the test PC.

### Attempt 2: UserNotificationListener

Windows notification listener access was requested and successfully changed to `Allowed`.

However, KakaoTalk messages still did not appear as usable notifications. Other apps such as Chrome and Windows Security appeared, but KakaoTalk message contents did not.

### Attempt 3: KakaoTalk popup window detection

The program detected KakaoTalk popup-related windows such as:

- `KakaoTalkShadowWndClass`
- `EVA_Window_Dblclk`
- `RICHEDIT50W`

UI Automation only exposed the quick reply input placeholder, such as `메시지 입력`, not the actual received message.

### Attempt 4: Popup image capture

The program tried to capture KakaoTalk popup areas and send them to OpenAI Vision.

This failed because the captured images were often black, stale, or only contained the quick reply input area.

### Attempt 5: Open chat window capture

The direction changed from notification/popup detection to reading the visible KakaoTalk chat window.

This worked partially, but `PrintWindow` returned stale chat content and GDI screen capture returned black images.

### Attempt 6: DPI-aware capture

Window coordinates were initially wrong because of Windows DPI scaling. The program was made DPI-aware, which fixed coordinate mismatches.

However, GDI capture still returned black images for KakaoTalk content.

### Attempt 7: DXGI capture with dxcam

The working solution was to use `dxcam`, which uses a DirectX/DXGI capture path.

This successfully captured the KakaoTalk chat window, allowing OpenAI image input to read the latest message and generate a reply draft.

## 7. Duplicate Prevention

The initial duplicate prevention used image hashes. This was unreliable because visually identical chat windows can produce different image bytes due to cursor blink, rendering differences, scroll bar changes, and capture timing.

The current implementation uses conversation content instead:

```text
room + sender + normalized_message
```

If the same room, sender, and message were processed within `kakao_chat_dedup_seconds`, the popup is skipped.

Image hashing still helps avoid processing identical captures during one run, but popup-level deduplication is now text-based.

## 8. Security Considerations

This program can handle private KakaoTalk messages and screenshots.

Important risks:

- Chat screenshots can be sent to OpenAI.
- Previous visible messages may be included in the screenshot.
- Generated messages can be saved to `data/messages.jsonl`.
- Test captures can remain in `data/chat_captures` or `data/popup_captures`.
- The project folder is under OneDrive in the test environment, so local data may sync.

The repository ignores `config.json` and `data/`, but users should still clean local runtime data when needed.

Recommended future improvements:

- Auto-delete old captures and logs
- Add `--no-save` for test modes
- Encrypt local logs
- Add sensitive-content detection before API calls
- Warn if `openai_base_url` is not the official OpenAI endpoint

## 9. Current Limitations

- The current working mode is personal chat oriented.
- Group chats are not fully supported yet.
- There is no `target_rooms` option yet.
- The chat window must be open and visible.
- The AI may occasionally misread the latest message from the image.
- Duplicate prevention happens after image interpretation, so repeated API calls can still happen if image hashes differ.
- The app drafts replies but does not and should not auto-send them.

## 10. Future Work

High-priority improvements:

1. Add `target_rooms` for group chat support.
2. In group chats, filter by both room title and visible sender name.
3. Add automatic cleanup for `data/` files.
4. Reduce the captured area before sending images to OpenAI.
5. Add sensitive keyword detection before API calls.
6. Add clipboard auto-clear after copying a draft.
7. Improve latest-message detection when several new messages arrive after the user's last message.
8. Add a small settings UI for target senders, reply style, and privacy options.
9. Add a safer base URL allowlist for OpenAI API calls.
10. Add integration tests around chat deduplication and popup regeneration.
