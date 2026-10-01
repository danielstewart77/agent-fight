"""Switch existing Agent Fight subscriptions after deployment, without exposing secrets.

Uses AZURE_DEVOPS_EXT_PAT. Defaults to a read-only preview; --apply makes changes.
The existing subscription supplies the webhook secret and endpoint options.
"""
import argparse
import os
import sys
from urllib.parse import quote, urlsplit

import httpx


COMMENT_EVENT = "ms.vss-code.git-pullrequest-comment-event"
OLD_EVENTS = {"git.pullrequest.created", "git.pullrequest.updated"}


def switch(client, organization, project_id, webhook_url, apply=False):
    base = f"https://dev.azure.com/{quote(organization, safe='')}/_apis/hooks/subscriptions"
    api = "api-version=7.1"

    def request(method, url, **kwargs):
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()

    subscriptions = request("GET", f"{base}?{api}")["value"]
    matching = [s for s in subscriptions
                if s.get("publisherInputs", {}).get("projectId") == project_id
                and s.get("consumerInputs", {}).get("url") == webhook_url
                and s.get("consumerId") == "webHooks"]
    old = [s for s in matching if s["eventType"] in OLD_EVENTS]
    comments = [s for s in matching if s["eventType"] == COMMENT_EVENT]
    if not old:
        raise ValueError("No matching legacy subscription to copy; check the project ID and exact webhook URL.")
    if len(comments) > 1:
        raise ValueError("Multiple matching comment subscriptions exist; resolve duplicates before switching.")
    template = old[0]
    if any(s["consumerInputs"] != template["consumerInputs"] for s in old[1:]):
        raise ValueError("Legacy subscriptions have different endpoint options; select matching settings first.")
    print(f"Comment subscription: {'reuse' if comments else 'create'}; legacy subscriptions to disable: {len(old)}")
    if not apply:
        print("Preview only. Deploy the new service, then repeat with --apply.")
        return

    endpoint = urlsplit(webhook_url)
    if endpoint.scheme != "https" or endpoint.username or endpoint.password:
        raise ValueError("An HTTPS webhook URL without embedded credentials is required.")
    health_url = f"{endpoint.scheme}://{endpoint.netloc}/health"
    # Never forward the ADO PAT to the service's health endpoint.
    health = request("GET", health_url, auth=None)
    if health.get("status") != "healthy" or health.get("review_trigger") != "pr_comment":
        raise ValueError("The new comment-command service is not deployed; subscriptions were not changed.")

    desired = {
        "publisherId": "tfs", "eventType": COMMENT_EVENT, "resourceVersion": "2.0",
        "consumerId": "webHooks", "consumerActionId": "httpRequest", "status": "enabled",
        "publisherInputs": {key: template["publisherInputs"].get(key, "")
                            for key in ("projectId", "repository", "branch")},
        "consumerInputs": template["consumerInputs"],
    }
    if comments:
        desired["id"] = comments[0]["id"]
        created = request("PUT", f"{base}/{desired['id']}?{api}", json=desired)
    else:
        created = request("POST", f"{base}?{api}", json=desired)
    check = request("GET", f"{base}/{created['id']}?{api}")
    if check.get("status") != "enabled" or check.get("eventType") != COMMENT_EVENT:
        raise RuntimeError("Comment subscription was not enabled; legacy subscriptions were not changed.")
    print(f"Comment subscription enabled: {created['id']}")
    for subscription in old:
        if subscription.get("status") == "disabledByUser":
            continue
        subscription["status"] = "disabledByUser"
        request("PUT", f"{base}/{subscription['id']}?{api}", json=subscription)
        print(f"Disabled {subscription['eventType']}: {subscription['id']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", required=True, help="ADO organization name, not URL")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--webhook-url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    pat = os.environ.get("AZURE_DEVOPS_EXT_PAT")
    if not pat:
        parser.error("Set AZURE_DEVOPS_EXT_PAT without including it on the command line.")
    try:
        with httpx.Client(auth=("", pat), timeout=30) as client:
            switch(client, args.organization, args.project_id, args.webhook_url, args.apply)
    except httpx.HTTPStatusError as exc:
        print(f"HTTP {exc.response.status_code}; switch did not finish. Check access and subscription state.", file=sys.stderr)
        return 1
    except httpx.RequestError:
        print("Network request failed; check connectivity and subscription state before retrying.", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
