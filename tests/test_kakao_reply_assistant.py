import unittest

from kakao_reply_assistant import (
    DEFAULT_CONFIG,
    chat_content_key,
    extract_openai_text,
    extract_texts_from_payload,
    recent_chat_message_seen,
    remember_recent_chat_message,
    parse_notification,
)


class PayloadParsingTests(unittest.TestCase):
    def test_extracts_toast_texts(self):
        payload = """
        <toast>
          <visual>
            <binding template="ToastGeneric">
              <text>홍길동</text>
              <text>오늘 저녁 가능해?</text>
            </binding>
          </visual>
        </toast>
        """

        self.assertEqual(extract_texts_from_payload(payload), ["홍길동", "오늘 저녁 가능해?"])

    def test_parse_matching_notification(self):
        config = dict(DEFAULT_CONFIG)
        config["target_senders"] = ["홍길동"]
        row = {
            "notification_id": 1,
            "handler_id": 10,
            "arrival_time": 123,
            "app_id": "KakaoTalk",
            "app_assets": "",
            "payload": "<toast><visual><binding><text>홍길동</text><text>오늘 저녁 가능해?</text></binding></visual></toast>",
        }

        parsed = parse_notification(row, config)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.sender, "홍길동")
        self.assertEqual(parsed.message, "오늘 저녁 가능해?")

    def test_parse_group_body_sender(self):
        config = dict(DEFAULT_CONFIG)
        config["target_senders"] = ["김민수"]
        row = {
            "notification_id": 2,
            "handler_id": 10,
            "arrival_time": 124,
            "app_id": "KakaoTalk",
            "app_assets": "",
            "payload": "<toast><visual><binding><text>친구방</text><text>김민수: 회의 끝났어?</text></binding></visual></toast>",
        }

        parsed = parse_notification(row, config)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.sender, "김민수")
        self.assertEqual(parsed.message, "회의 끝났어?")


class OpenAIResponseParsingTests(unittest.TestCase):
    def test_extracts_output_text_property(self):
        self.assertEqual(extract_openai_text({"output_text": "좋아, 이따 봐!"}), "좋아, 이따 봐!")

    def test_extracts_output_array(self):
        response = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": "응, 오늘 저녁 가능해."},
                    ]
                }
            ]
        }

        self.assertEqual(extract_openai_text(response), "응, 오늘 저녁 가능해.")


class ChatDedupTests(unittest.TestCase):
    def test_chat_content_key_normalizes_punctuation_spacing(self):
        self.assertEqual(
            chat_content_key("홍길동", "홍길동", "내일 시간 괜찮아 ?"),
            chat_content_key(" 홍길동 ", "홍길동", "내일 시간 괜찮아?"),
        )

    def test_recent_chat_message_seen_within_window(self):
        records = remember_recent_chat_message([], "홍길동", "홍길동", "내일 시간 괜찮아?", 1000.0, 300.0)

        self.assertTrue(recent_chat_message_seen(records, "홍길동", "홍길동", "내일 시간 괜찮아 ?", 1100.0, 300.0))
        self.assertFalse(recent_chat_message_seen(records, "홍길동", "홍길동", "내일 시간 괜찮아?", 1401.0, 300.0))


if __name__ == "__main__":
    unittest.main()
