from app.messages import welcome_message


def test_welcome_message_is_spelled_correctly() -> None:
    assert welcome_message() == "Welcome to AI Platform"
