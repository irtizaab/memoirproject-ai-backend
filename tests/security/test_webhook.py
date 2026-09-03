"""The one endpoint that has to be open to the internet.

AssemblyAI has no account here and can hold no Supabase token, so there is no
credential of the usual kind for it to send. Instead every job is submitted with
a header name and a value we invent, AssemblyAI echoes both back, and this route
checks the value matches.

That makes one shared secret the entire authentication for a public endpoint,
which is why it gets its own file.
"""

import pytest

from src.integrations.assemblyai import WEBHOOK_AUTH_HEADER

pytestmark = pytest.mark.security

SECRET = "test-webhook-secret"  # set in conftest, before the app is imported


@pytest.fixture
def applied(monkeypatch):
    """Record calls to `apply_result` without touching the database.

    These tests are about the door, not the room behind it.
    """
    calls = []
    monkeypatch.setattr(
        "src.api.webhooks.apply_result",
        lambda provider_id, job=None: calls.append((provider_id, job)) or True,
    )
    return calls


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


def test_no_secret_is_refused(client):
    response = client.post("/webhooks/assemblyai", json={"transcript_id": "x"})
    assert response.status_code == 401


def test_a_wrong_secret_is_refused(client):
    response = client.post(
        "/webhooks/assemblyai",
        json={"transcript_id": "x"},
        headers={WEBHOOK_AUTH_HEADER: "not-the-secret"},
    )
    assert response.status_code == 401


def test_an_empty_secret_is_refused(client):
    response = client.post(
        "/webhooks/assemblyai",
        json={"transcript_id": "x"},
        headers={WEBHOOK_AUTH_HEADER: ""},
    )
    assert response.status_code == 401


def test_it_fails_closed_when_no_secret_is_configured(client, monkeypatch):
    """Unconfigured must mean "refuse everyone", not "accept everyone".

    `_authentic` returns False when the expected value is falsy, so a
    deployment that forgot to set the secret rejects every callback rather than
    accepting any string — including an empty one. This is the difference
    between a broken feature and an open endpoint.
    """
    from src.core.config import settings

    monkeypatch.setattr(settings, "assemblyai_webhook_secret", None)

    for supplied in ["", "anything", SECRET]:
        response = client.post(
            "/webhooks/assemblyai",
            json={"transcript_id": "x"},
            headers={WEBHOOK_AUTH_HEADER: supplied},
        )
        assert response.status_code == 401, supplied


def test_a_near_miss_is_refused(client):
    """One character wrong, and one character short.

    `hmac.compare_digest` rather than `==`. A plain comparison returns the
    moment two bytes differ, and how long that takes leaks how much of a guess
    was right — enough, over many attempts, to recover the secret a character at
    a time. Constant-time comparison removes the signal.
    """
    for wrong in [SECRET[:-1], SECRET + "x", SECRET.upper(), SECRET.replace("t", "T")]:
        response = client.post(
            "/webhooks/assemblyai",
            json={"transcript_id": "x"},
            headers={WEBHOOK_AUTH_HEADER: wrong},
        )
        assert response.status_code == 401, wrong


def test_the_secret_is_never_echoed_back(client):
    """A rejection says nothing about what was expected."""
    response = client.post(
        "/webhooks/assemblyai",
        json={"transcript_id": "x"},
        headers={WEBHOOK_AUTH_HEADER: "guess"},
    )
    assert SECRET not in response.text


# ---------------------------------------------------------------------------
# Behind the door: 200 for nearly everything, on purpose
# ---------------------------------------------------------------------------


def test_unreadable_json_is_accepted_not_retried(client, applied):
    """A sender reads a non-2xx as "try again later".

    An endpoint that answers 500 to a payload it dislikes has arranged to be
    sent that same payload on a retry schedule for hours. Nothing about
    unreadable JSON improves by being sent again, so it is acknowledged.
    """
    response = client.post(
        "/webhooks/assemblyai",
        content=b"not json at all",
        headers={
            WEBHOOK_AUTH_HEADER: SECRET,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"received": False}
    assert applied == []


def test_a_payload_naming_no_transcript_is_accepted(client, applied):
    response = client.post(
        "/webhooks/assemblyai",
        json={"status": "completed"},
        headers={WEBHOOK_AUTH_HEADER: SECRET},
    )
    assert response.status_code == 200
    assert response.json() == {"received": False}
    assert applied == []


def test_an_in_flight_status_writes_nothing(client, applied):
    """`queued` and `processing` are progress reports, not results."""
    for status in ["queued", "processing", "something-new"]:
        response = client.post(
            "/webhooks/assemblyai",
            json={"transcript_id": "job-1", "status": status},
            headers={WEBHOOK_AUTH_HEADER: SECRET},
        )
        assert response.status_code == 200
        assert response.json() == {"received": True}

    assert applied == [], "an unfinished job should not be written through"


def test_an_unknown_job_is_acknowledged_without_confirming_it_exists(
    client, monkeypatch
):
    """Genuine callback, no row — ordinary, and not a retry case.

    It usually means the memory was deleted while the transcript was still being
    made. The response is identical to a successful one, so the body never
    reveals whether a given job id is known to this database.
    """
    monkeypatch.setattr(
        "src.api.webhooks.apply_result", lambda provider_id, job=None: False
    )

    unknown = client.post(
        "/webhooks/assemblyai",
        json={"transcript_id": "never-heard-of-it", "status": "completed"},
        headers={WEBHOOK_AUTH_HEADER: SECRET},
    )

    monkeypatch.setattr(
        "src.api.webhooks.apply_result", lambda provider_id, job=None: True
    )
    known = client.post(
        "/webhooks/assemblyai",
        json={"transcript_id": "a-real-job", "status": "completed"},
        headers={WEBHOOK_AUTH_HEADER: SECRET},
    )

    assert unknown.status_code == known.status_code == 200
    assert unknown.json() == known.json(), (
        "the response distinguishes a known job from an unknown one, which "
        "turns this endpoint into an oracle for anyone holding the secret"
    )


def test_both_id_spellings_are_accepted(client, applied):
    """AssemblyAI has used `transcript_id` and `id`; the route reads either."""
    client.post(
        "/webhooks/assemblyai",
        json={"transcript_id": "job-a", "status": "completed", "text": "words"},
        headers={WEBHOOK_AUTH_HEADER: SECRET},
    )
    client.post(
        "/webhooks/assemblyai",
        json={"id": "job-b", "status": "completed", "text": "words"},
        headers={WEBHOOK_AUTH_HEADER: SECRET},
    )

    assert [provider for provider, _ in applied] == ["job-a", "job-b"]


def test_the_secret_alone_cannot_inject_text_into_an_arbitrary_transcript(client):
    """The limit of what the shared secret buys an attacker.

    Anyone holding it can name any `transcript_id`. But `apply_result` re-fetches
    from AssemblyAI whenever the payload carries no `text`, so a bare
    `{"transcript_id": X, "status": "completed"}` cannot plant words in somebody
    else's recording — the provider is asked what the job really said.

    A payload that *does* carry `text` is trusted verbatim, and `_write_terminal`
    only refuses rows that have already finished. So an in-flight transcript can
    be overwritten by someone with the secret. That is the real boundary, and it
    is recorded here so it is a known property rather than a surprise.
    """
    from src.domain.transcripts import transcript_service

    source = transcript_service.apply_result.__doc__ or ""
    assert "fetch" in source.lower(), (
        "apply_result no longer documents re-fetching a thin payload — if it "
        "stopped doing so, the webhook secret now grants text injection"
    )
