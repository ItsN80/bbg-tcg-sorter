"""Read-Card.py's DO Serverless handling: sends the game id, refuses custom
packs before uploading, and turns the server's 400 into a hard error."""
import importlib.util
import json
import os
import sys
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import games  # noqa: E402

spec = importlib.util.spec_from_file_location("read_card", os.path.join(REPO, "scripts", "Read-Card.py"))
read_card = importlib.util.module_from_spec(spec)
spec.loader.exec_module(read_card)

DO_CONFIG = {"recognition_provider": "do_serverless",
             "ollama": {"base_url": "https://do.example", "api_key": "k"}}


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "card.jpg"
    p.write_bytes(b"jpeg")
    return str(p)


def _response(status, body):
    r = mock.MagicMock(status_code=status, text=json.dumps(body))
    r.json.return_value = body
    r.raise_for_status.side_effect = None if status < 400 else Exception(f"HTTP {status}")
    return r


def test_sends_game_and_parses_fab(image):
    fab = games.get_game("fab", strict=True)
    body = {"response": '```json\n{"card_name": "Spore Port", "card_id": "MON280", "pitch": null, "confidence": 0.9}\n```'}
    with mock.patch.object(read_card.requests, "post", return_value=_response(200, body)) as post:
        result = read_card.recognize_with_ollama(image, DO_CONFIG, fab)
    assert post.call_args.kwargs["json"]["game"] == "fab"
    assert result == {"card_name": "Spore Port", "card_id": "MON280", "pitch": "Unknown"}


def test_mtg_server_prompt_shape_is_accepted(image):
    """The server's MTG prompt answers with "name" and no confidence."""
    mtg = games.get_game("mtg", strict=True)
    body = {"response": '{"name": "Wizened Crone", "set_code": null, "collector_number": null}'}
    with mock.patch.object(read_card.requests, "post", return_value=_response(200, body)) as post:
        result = read_card.recognize_with_ollama(image, DO_CONFIG, mtg)
    assert post.call_args.kwargs["json"]["game"] == "mtg"
    assert result["card_name"] == "Wizened Crone"


def test_direct_ollama_does_not_send_game(image):
    mtg = games.get_game("mtg", strict=True)
    cfg = dict(DO_CONFIG, recognition_provider="ollama")
    with mock.patch.object(read_card.requests, "post",
                           return_value=_response(200, {"response": '{"card_name": "Opt"}'})) as post:
        read_card.recognize_with_ollama(image, cfg, mtg)
    assert "game" not in post.call_args.kwargs["json"]


def test_custom_pack_never_uploaded_to_do(image):
    custom = dict(games.get_game("mtg", strict=True), id="custom", name="Custom Game", _builtin=False)
    with mock.patch.object(read_card.requests, "post") as post:
        with pytest.raises(read_card.UnsupportedGameError):
            read_card.recognize_with_ollama(image, DO_CONFIG, custom)
    assert not post.called


def test_server_400_unsupported_game_is_a_hard_error(image):
    fab = games.get_game("fab", strict=True)
    with mock.patch.object(read_card.requests, "post",
                           return_value=_response(400, {"error": "unsupported game 'fab'"})) as post:
        with pytest.raises(read_card.UnsupportedGameError):
            read_card.recognize_with_ollama(image, DO_CONFIG, fab)
    assert post.call_count == 1  # no retry
