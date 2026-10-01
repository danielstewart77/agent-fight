"""A subscription switch must never disable delivery before the replacement is ready."""
import json

import httpx
import pytest

from scripts.switch_comment_hook import COMMENT_EVENT, switch


@pytest.mark.parametrize("mode", ["preview", "old_service", "apply"])
def test_switch_checks_deployment_and_enables_comments_before_disabling_old_hooks(mode):
    writes = []
    template = {
        "id": "old", "eventType": "git.pullrequest.created", "status": "enabled",
        "publisherId": "tfs", "consumerId": "webHooks", "consumerActionId": "httpRequest",
        "publisherInputs": {"projectId": "project", "repository": "repo", "branch": "", "pullrequestCreatedBy": "group"},
        "consumerInputs": {"url": "https://service.test/webhooks/ado", "httpHeaders": "secret"},
    }
    def handler(request):
        if request.url.host == "service.test":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"status": "healthy", "review_trigger":
                                           "old" if mode == "old_service" else "pr_comment"})
        if request.method == "GET":
            return httpx.Response(200, json={"status": "enabled", "eventType": COMMENT_EVENT}
                                 if request.url.path.endswith("/new") else {"value": [template]})
        body = json.loads(request.content)
        writes.append(body)
        return httpx.Response(200, json={"id": "new"})
    with httpx.Client(auth=("", "pat"), transport=httpx.MockTransport(handler)) as client:
        if mode == "old_service":
            with pytest.raises(ValueError, match="not deployed"):
                switch(client, "org", "project", "https://service.test/webhooks/ado", True)
        else:
            switch(client, "org", "project", "https://service.test/webhooks/ado", mode == "apply")
    if mode != "apply":
        assert writes == []
    else:
        assert writes[0]["eventType"] == COMMENT_EVENT
        assert writes[0]["consumerInputs"] == template["consumerInputs"]
        assert writes[0]["publisherInputs"] == {"projectId": "project", "repository": "repo", "branch": ""}
        assert writes[1]["id"] == "old"
        assert writes[1]["status"] == "disabledByUser"
