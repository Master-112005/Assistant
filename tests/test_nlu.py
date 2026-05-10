from core.nlu import extract_entities


def test_extract_send_message_entities_from_say_to_phrase():
    entities = extract_entities("send_message", "say hi to hemanth")

    assert entities["contact"] == "hemanth"
    assert entities["message"] == "hi"


def test_extract_send_message_entities_from_tell_phrase():
    entities = extract_entities("send_message", "tell hemanth meeting moved to 5")

    assert entities["contact"] == "hemanth"
    assert entities["message"] == "meeting moved to 5"
