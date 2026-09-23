from flask import Flask, jsonify, request
import html
import json
import logging
import os
import re
from urllib.parse import quote

import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("topdesk-proxy-v3")

TOPDESK_USER = os.getenv("TOPDESK_USER")
TOPDESK_TOKEN = os.getenv("TOPDESK_TOKEN")
TOPDESK_HOST = os.getenv("TOPDESK_HOST", "https://saether.topdesk.net").rstrip("/")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

KB_BASE = f"{TOPDESK_HOST}/services/knowledge-base-v1"
GENERAL_BASE = f"{TOPDESK_HOST}/tas/api"
SERVICES_BASE = f"{TOPDESK_HOST}/services/service-v1"

KB_FIELDS = "title,description,content,keywords,urls,modificationDate,availableTranslations"
INCIDENT_FIELDS = (
    "id,number,briefDescription,request,action,creationDate,modificationDate,"
    "targetDate,closedDate,status,caller,operator,operatorGroup,category,"
    "subcategory,callType,priority,urgency,impact,branch,location,object"
)


def require_proxy_key():
    if not PROXY_API_KEY:
        return jsonify({
            "error": "Configuration error",
            "message": "PROXY_API_KEY is not configured on the server."
        }), 500

    supplied = request.headers.get("X-API-Key", "")
    if supplied != PROXY_API_KEY:
        return jsonify({
            "error": "Unauthorized",
            "message": "Missing or invalid X-API-Key."
        }), 401

    return None


@app.before_request
def before_request():
    logger.info(
        "%s %s query=%s",
        request.method,
        request.path,
        request.query_string.decode("utf-8", errors="replace")
    )

    if request.path in {"/", "/health", "/swagger.json"}:
        return None

    return require_proxy_key()


def validate_config():
    missing = []
    if not TOPDESK_USER:
        missing.append("TOPDESK_USER")
    if not TOPDESK_TOKEN:
        missing.append("TOPDESK_TOKEN")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def clean_html(value):
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    text = re.sub(r"</\s*(p|div|li|ol|ul|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<\s*img\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def scalar(value):
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or value.get("id") or "")
    return str(value or "")


def topdesk_get(base, path, params=None, accept="application/json"):
    validate_config()
    response = requests.get(
        f"{base}{path}",
        params=params,
        auth=(TOPDESK_USER, TOPDESK_TOKEN),
        headers={"Accept": accept},
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


def kb_get(path, params=None):
    return topdesk_get(
        KB_BASE,
        path,
        params=params,
        accept=(
            "application/x.topdesk-kb-ki-list-v1+json, "
            "application/x.topdesk-kb-ki-v1+json, application/json"
        )
    )


def general_get(path, params=None):
    return topdesk_get(GENERAL_BASE, path, params=params)


def services_get(path, params=None):
    return topdesk_get(SERVICES_BASE, path, params=params)


def error_response(error, label):
    status = error.response.status_code if getattr(error, "response", None) is not None else 502
    message = error.response.text if getattr(error, "response", None) is not None else str(error)
    return jsonify({"error": label, "statusCode": status, "message": message}), status


def safe_endpoint(func):
    try:
        return func()
    except requests.HTTPError as error:
        return error_response(error, "TOPdesk request failed")
    except requests.RequestException as error:
        return jsonify({"error": "TOPdesk connection failed", "message": str(error)}), 502
    except RuntimeError as error:
        return jsonify({"error": "Configuration error", "message": str(error)}), 500


def translation_content(item):
    return item.get("translation", {}).get("content", {})


def transform_knowledge(item):
    content = translation_content(item)
    return {
        "id": str(item.get("id") or ""),
        "number": str(item.get("number") or ""),
        "title": clean_html(content.get("title", "")),
        "description": clean_html(content.get("description", "")),
        "content": clean_html(content.get("content", "")),
        "keywords": clean_html(content.get("keywords", "")),
        "modificationDate": str(item.get("modificationDate") or ""),
        "availableTranslations": item.get("availableTranslations") or [],
        "urls": item.get("urls") or {}
    }


def transform_incident(item):
    transformed = {
        "id": str(item.get("id") or ""),
        "number": str(item.get("number") or ""),
        "briefDescription": clean_html(item.get("briefDescription", "")),
        "request": clean_html(item.get("request", "")),
        "action": clean_html(item.get("action", "")),
        "creationDate": str(item.get("creationDate") or ""),
        "modificationDate": str(item.get("modificationDate") or ""),
        "targetDate": str(item.get("targetDate") or ""),
        "closedDate": str(item.get("closedDate") or "")
    }
    for name in (
        "status", "caller", "operator", "operatorGroup", "category", "subcategory",
        "callType", "priority", "urgency", "impact", "branch", "location", "object"
    ):
        transformed[name] = scalar(item.get(name))
    return transformed


def extract_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "item", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def tokenize(query):
    return [
        part.lower()
        for part in re.findall(r"[\wæøåÆØÅ-]+", query or "")
        if len(part) > 1
    ]


def incident_score(item, terms):
    weighted = (
        (item.get("number", "").lower(), 100),
        (item.get("briefDescription", "").lower(), 30),
        (item.get("request", "").lower(), 20),
        (item.get("action", "").lower(), 12),
        (item.get("category", "").lower(), 10),
        (item.get("subcategory", "").lower(), 10),
        (item.get("status", "").lower(), 8),
        (item.get("caller", "").lower(), 6),
        (item.get("operator", "").lower(), 6),
        (item.get("operatorGroup", "").lower(), 6)
    )
    return sum(weight for term in terms for text, weight in weighted if term in text)


def knowledge_score(item, terms):
    weighted = (
        (item.get("number", "").lower(), 100),
        (item.get("title", "").lower(), 25),
        (item.get("keywords", "").lower(), 20),
        (item.get("description", "").lower(), 12),
        (item.get("content", "").lower(), 8)
    )
    return sum(weight for term in terms for text, weight in weighted if term in text)


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "TOPdesk Copilot Proxy V3",
        "version": "3.0.1",
        "swagger": "/swagger.json"
    })


@app.get("/health")
def health():
    missing = []
    if not TOPDESK_USER:
        missing.append("TOPDESK_USER")
    if not TOPDESK_TOKEN:
        missing.append("TOPDESK_TOKEN")
    if not PROXY_API_KEY:
        missing.append("PROXY_API_KEY")
    return jsonify({
        "status": "ok" if not missing else "configuration_error",
        "missingVariables": missing
    }), 200 if not missing else 500


@app.get("/knowledge/search")
def search_knowledge():
    def run():
        query = request.args.get("query", "").strip()
        limit = max(1, min(request.args.get("limit", 7, type=int), 10))
        if not query:
            return jsonify({"error": "Missing query", "message": "Supply query."}), 400
        data = kb_get(
            "/knowledgeItems",
            params={"start": 0, "page_size": 1000, "fields": KB_FIELDS}
        )
        items = [transform_knowledge(item) for item in extract_list(data)]
        terms = tokenize(query)
        results = []
        for item in items:
            score = knowledge_score(item, terms)
            if score > 0:
                item["relevanceScore"] = score
                results.append(item)
        results.sort(
            key=lambda x: (x["relevanceScore"], x.get("modificationDate", "")),
            reverse=True
        )
        return jsonify({
            "query": query,
            "count": len(results[:limit]),
            "references": results[:limit]
        })
    return safe_endpoint(run)


@app.get("/knowledge/items")
def list_knowledge_items():
    def run():
        start = max(0, request.args.get("start", 0, type=int))
        page_size = max(1, min(request.args.get("pageSize", 25, type=int), 1000))
        data = kb_get(
            "/knowledgeItems",
            params={"start": start, "page_size": page_size, "fields": KB_FIELDS}
        )
        items = [transform_knowledge(item) for item in extract_list(data)]
        return jsonify({
            "count": len(items),
            "start": start,
            "pageSize": page_size,
            "items": items,
            "hasNextPage": bool(data.get("next")) if isinstance(data, dict) else False
        })
    return safe_endpoint(run)


@app.get("/knowledge/items/<path:identifier>")
def get_knowledge_item(identifier):
    return safe_endpoint(
        lambda: jsonify(
            transform_knowledge(
                kb_get(
                    f"/knowledgeItems/{quote(identifier, safe='')}",
                    params={"fields": KB_FIELDS}
                )
            )
        )
    )


@app.get("/knowledge/statuses")
def knowledge_statuses():
    return safe_endpoint(lambda: jsonify(kb_get("/knowledgeItemStatuses")))


@app.get("/knowledge/explorer-migrated")
def explorer_migrated():
    return safe_endpoint(lambda: jsonify(kb_get("/explorer/migrated")))


@app.get("/incidents")
def list_incidents():
    def run():
        start = max(0, request.args.get("start", 0, type=int))
        limit = max(1, min(request.args.get("limit", 10, type=int), 100))
        status = request.args.get("status", "").strip().lower()
        data = general_get(
            "/incidents",
            params={
                "pageStart": start,
                "pageSize": limit,
                "sort": "creationDate:desc",
                "dateFormat": "iso8601",
                "fields": INCIDENT_FIELDS
            }
        )
        items = [transform_incident(item) for item in extract_list(data)]
        if status:
            items = [item for item in items if status in item.get("status", "").lower()]
        return jsonify({
            "count": len(items),
            "start": start,
            "limit": limit,
            "incidents": items
        })
    return safe_endpoint(run)


@app.get("/incidents/search")
def search_incidents():
    def run():
        query = request.args.get("query", "").strip()
        limit = max(5, min(request.args.get("limit", 7, type=int), 10))
        scan = max(25, min(request.args.get("scan", 250, type=int), 1000))
        if not query:
            return jsonify({"error": "Missing query", "message": "Supply query."}), 400
        data = general_get(
            "/incidents",
            params={
                "pageStart": 0,
                "pageSize": scan,
                "sort": "creationDate:desc",
                "dateFormat": "iso8601",
                "fields": INCIDENT_FIELDS
            }
        )
        incidents = [transform_incident(item) for item in extract_list(data)]
        terms = tokenize(query)
        results = []
        for incident in incidents:
            score = incident_score(incident, terms)
            if score > 0:
                incident["relevanceScore"] = score
                results.append(incident)
        results.sort(
            key=lambda x: (x["relevanceScore"], x.get("creationDate", "")),
            reverse=True
        )
        return jsonify({
            "query": query,
            "scanned": len(incidents),
            "count": len(results[:limit]),
            "references": results[:limit]
        })
    return safe_endpoint(run)


@app.get("/incidents/id/<path:incident_id>")
def get_incident_by_id(incident_id):
    return safe_endpoint(
        lambda: jsonify(
            transform_incident(
                general_get(
                    f"/incidents/id/{quote(incident_id, safe='')}",
                    params={"dateFormat": "iso8601"}
                )
            )
        )
    )


@app.get("/incidents/number/<path:number>")
def get_incident_by_number(number):
    return safe_endpoint(
        lambda: jsonify(
            transform_incident(
                general_get(
                    f"/incidents/number/{quote(number, safe='')}",
                    params={"dateFormat": "iso8601"}
                )
            )
        )
    )


def lookup(path):
    return safe_endpoint(lambda: jsonify(general_get(path)))


@app.get("/lookups/incidents/statuses")
def incident_statuses(): return lookup("/incidents/statuses")

@app.get("/lookups/incidents/categories")
def incident_categories(): return lookup("/incidents/categories")

@app.get("/lookups/incidents/subcategories")
def incident_subcategories(): return lookup("/incidents/subcategories")

@app.get("/lookups/incidents/priorities")
def incident_priorities(): return lookup("/incidents/priorities")

@app.get("/lookups/incidents/urgencies")
def incident_urgencies(): return lookup("/incidents/urgencies")

@app.get("/lookups/incidents/impacts")
def incident_impacts(): return lookup("/incidents/impacts")

@app.get("/lookups/incidents/call-types")
def incident_call_types(): return lookup("/incidents/call_types")

@app.get("/lookups/incidents/entry-types")
def incident_entry_types(): return lookup("/incidents/entry_types")

@app.get("/lookups/incidents/durations")
def incident_durations(): return lookup("/incidents/durations")

@app.get("/lookups/incidents/operator-groups")
def incident_operator_groups(): return lookup("/incidents/operatorgroups/lookup")

@app.get("/lookups/incidents/operators")
def incident_operators(): return lookup("/incidents/operators/lookup")

@app.get("/lookups/incidents/callers")
def incident_callers(): return lookup("/incidents/callers/lookup")

@app.get("/lookups/incidents/closure-codes")
def incident_closure_codes(): return lookup("/incidents/closure_codes")

@app.get("/lookups/incidents/slas")
def incident_slas(): return lookup("/incidents/slas")


@app.get("/general/search")
def general_search():
    def run():
        params = {
            key: value
            for key, value in request.args.items()
            if key in {"query", "pageStart", "pageSize", "sort"}
        }
        return jsonify(general_get("/search", params=params))
    return safe_endpoint(run)


@app.get("/general/archiving-reasons")
def archiving_reasons(): return lookup("/archiving-reasons")

@app.get("/general/timespent-reasons")
def timespent_reasons(): return lookup("/timespent-reasons")

@app.get("/general/version")
def api_version(): return lookup("/version")

@app.get("/general/product-version")
def product_version(): return lookup("/productVersion")

@app.get("/general/categories")
def general_categories(): return lookup("/categories")

@app.get("/general/requester-categories")
def requester_categories(): return lookup("/requester/categories")

@app.get("/general/service-windows")
def service_windows(): return lookup("/serviceWindow/lookup")

@app.get("/general/service-windows/<path:window_id>")
def service_window(window_id):
    return lookup(f"/serviceWindow/lookup/{quote(window_id, safe='')}")

@app.get("/general/emails/<path:email_id>")
def get_email(email_id):
    return lookup(f"/emails/id/{quote(email_id, safe='')}")


@app.get("/services")
def list_services():
    return safe_endpoint(
        lambda: jsonify(services_get("/services", params=dict(request.args)))
    )


@app.get("/services/<path:service_id>")
def get_service(service_id):
    return safe_endpoint(
        lambda: jsonify(
            services_get(f"/services/{quote(service_id, safe='')}")
        )
    )


@app.get("/services/<path:service_id>/linked-assets")
def linked_assets(service_id):
    return safe_endpoint(
        lambda: jsonify(
            services_get(
                f"/services/{quote(service_id, safe='')}/linkedAssets"
            )
        )
    )


@app.get("/swagger.json")
def swagger_json():
    swagger_path = os.path.join(os.path.dirname(__file__), "swagger.json")
    with open(swagger_path, "r", encoding="utf-8") as file:
        return jsonify(json.load(file))


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found", "swagger": "/swagger.json"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
