from flask import Flask, jsonify, request
import hmac
import html
import logging
import os
import re

import requests


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("topdesk-proxy")


# ---------------------------------------------------------
# Environment variables
# ---------------------------------------------------------

TOPDESK_USER = os.getenv("TOPDESK_USER")
TOPDESK_TOKEN = os.getenv("TOPDESK_TOKEN")
PROXY_API_KEY = os.getenv("PROXY_API_KEY")


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

TOPDESK_BASE_URL = (
    "https://saether.topdesk.net/"
    "services/knowledge-base-v1"
)

REQUEST_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
DEFAULT_RESULT_LIMIT = 10
MAX_RESULT_LIMIT = 25

FIELDS = (
    "title,"
    "description,"
    "content,"
    "keywords,"
    "urls,"
    "modificationDate,"
    "availableTranslations"
)


# ---------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------

def get_missing_configuration():
    missing = []

    if not TOPDESK_USER:
        missing.append("TOPDESK_USER")

    if not TOPDESK_TOKEN:
        missing.append("TOPDESK_TOKEN")

    if not PROXY_API_KEY:
        missing.append("PROXY_API_KEY")

    return missing


# ---------------------------------------------------------
# Safe request diagnostics
# ---------------------------------------------------------

@app.before_request
def authorize_request():
    return None


# ---------------------------------------------------------
# Proxy API authentication
# ---------------------------------------------------------

def verify_proxy_api_key():
    supplied_key = request.headers.get("X-API-Key", "")

    if not PROXY_API_KEY:
        return False

    return hmac.compare_digest(
        supplied_key,
        PROXY_API_KEY
    )


@app.before_request
def authorize_request():
    public_paths = {
        "/",
        "/health",
        "/swagger.json"
    }

    if request.path in public_paths:
        return None

    if not verify_proxy_api_key():
        logger.warning(
            "Unauthorized request path=%s x_api_key_present=%s "
            "x_api_key_length=%s configured_key_present=%s",
            request.path,
            bool(request.headers.get("X-API-Key", "")),
            len(request.headers.get("X-API-Key", "")),
            bool(PROXY_API_KEY),
        )

        return jsonify({
            "error": "Unauthorized",
            "message": "A valid X-API-Key header is required."
        }), 401

    return None


# ---------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------

def clean_html(value):
    if not value:
        return ""

    text = html.unescape(str(value))

    text = re.sub(
        r"<\s*br\s*/?\s*>",
        "\n",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"</\s*(p|div|li|ol|ul|h[1-6])\s*>",
        "\n",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"<\s*img\b[^>]*>",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)

    return text.strip()


# ---------------------------------------------------------
# TOPdesk data transformation
# ---------------------------------------------------------

def get_translation_content(item):
    return (
        item
        .get("translation", {})
        .get("content", {})
    )


def transform_item(item):
    translation_content = get_translation_content(item)

    return {
        "id": item.get("id", ""),
        "number": item.get("number", ""),
        "title": clean_html(translation_content.get("title", "")),
        "description": clean_html(
            translation_content.get("description", "")
        ),
        "content": clean_html(translation_content.get("content", "")),
        "keywords": clean_html(translation_content.get("keywords", "")),
        "urls": item.get("urls", {}),
        "modificationDate": item.get("modificationDate", ""),
        "availableTranslations": item.get("availableTranslations", [])
    }


# ---------------------------------------------------------
# TOPdesk HTTP client
# ---------------------------------------------------------

def topdesk_get(path, params=None):
    missing = get_missing_configuration()

    if missing:
        raise RuntimeError(
            "Missing environment variables: " + ", ".join(missing)
        )

    response = requests.get(
        f"{TOPDESK_BASE_URL}{path}",
        params=params,
        auth=(TOPDESK_USER, TOPDESK_TOKEN),
        headers={
            "Accept": (
                "application/x.topdesk-kb-ki-list-v1+json, "
                "application/x.topdesk-kb-ki-v1+json, "
                "application/json"
            )
        },
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()
    return response.json()


def get_all_knowledge_items():
    all_items = []
    start = 0

    while True:
        data = topdesk_get(
            "/knowledgeItems",
            params={
                "start": start,
                "page_size": MAX_PAGE_SIZE,
                "fields": FIELDS
            }
        )

        page_items = data.get("item", [])
        all_items.extend(page_items)

        if not data.get("next") or not page_items:
            break

        start += len(page_items)

    return all_items


# ---------------------------------------------------------
# Search scoring
# ---------------------------------------------------------

def calculate_score(item, search_terms):
    title = item.get("title", "").lower()
    description = item.get("description", "").lower()
    content = item.get("content", "").lower()
    keywords = item.get("keywords", "").lower()
    number = item.get("number", "").lower()

    score = 0

    for term in search_terms:
        if term in number:
            score += 100
        if term in title:
            score += 20
        if term in keywords:
            score += 15
        if term in description:
            score += 8
        if term in content:
            score += 3

    return score


# ---------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------

@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "TOPdesk Knowledge Base Proxy",
        "description": (
            "Provides live search and retrieval of TOPdesk Knowledge Items."
        ),
        "endpoints": {
            "health": "/health",
            "search": "/search?q=helpdesk",
            "specificItem": "/knowledge-items/KI%200080",
            "swagger": "/swagger.json"
        }
    })


@app.get("/health")
def health():
    missing = get_missing_configuration()

    if missing:
        return jsonify({
            "status": "configuration_error",
            "missingVariables": missing
        }), 500

    return jsonify({
        "status": "ok",
        "service": "TOPdesk Knowledge Base Proxy"
    })


# ---------------------------------------------------------
# List Knowledge Items
# ---------------------------------------------------------

@app.get("/knowledge-items")
def list_knowledge_items():
    try:
        page_size = request.args.get(
            "page_size",
            DEFAULT_PAGE_SIZE,
            type=int
        )
        start = request.args.get("start", 0, type=int)

        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        start = max(0, start)

        data = topdesk_get(
            "/knowledgeItems",
            params={
                "start": start,
                "page_size": page_size,
                "fields": FIELDS
            }
        )

        items = [
            transform_item(item)
            for item in data.get("item", [])
        ]

        return jsonify({
            "items": items,
            "count": len(items),
            "start": start,
            "pageSize": page_size,
            "hasNextPage": bool(data.get("next"))
        })

    except requests.HTTPError as error:
        status_code = (
            error.response.status_code
            if error.response is not None
            else 502
        )
        response_text = (
            error.response.text
            if error.response is not None
            else str(error)
        )
        return jsonify({
            "error": "TOPdesk request failed",
            "statusCode": status_code,
            "message": response_text
        }), status_code

    except requests.RequestException as error:
        return jsonify({
            "error": "TOPdesk connection failed",
            "message": str(error)
        }), 502

    except RuntimeError as error:
        return jsonify({
            "error": "Configuration error",
            "message": str(error)
        }), 500


# ---------------------------------------------------------
# Get one specific Knowledge Item
# ---------------------------------------------------------

@app.get("/knowledge-items/<path:identifier>")
def get_knowledge_item(identifier):
    try:
        data = topdesk_get(
            f"/knowledgeItems/{identifier}",
            params={"fields": FIELDS}
        )

        return jsonify(transform_item(data))

    except requests.HTTPError as error:
        status_code = (
            error.response.status_code
            if error.response is not None
            else 502
        )
        response_text = (
            error.response.text
            if error.response is not None
            else str(error)
        )
        return jsonify({
            "error": "TOPdesk request failed",
            "statusCode": status_code,
            "message": response_text
        }), status_code

    except requests.RequestException as error:
        return jsonify({
            "error": "TOPdesk connection failed",
            "message": str(error)
        }), 502

    except RuntimeError as error:
        return jsonify({
            "error": "Configuration error",
            "message": str(error)
        }), 500


# ---------------------------------------------------------
# Search Knowledge Items
# ---------------------------------------------------------

@app.get("/search")
def search_knowledge_items():
    query = request.args.get("q", "").strip()
    limit = request.args.get(
        "limit",
        DEFAULT_RESULT_LIMIT,
        type=int
    )
    limit = max(1, min(limit, MAX_RESULT_LIMIT))

    if not query:
        return jsonify({
            "error": "Missing query",
            "message": "Supply a search term with the q parameter."
        }), 400

    search_terms = [
        term.lower()
        for term in query.split()
        if term.strip()
    ]

    try:
        raw_items = get_all_knowledge_items()
        transformed_items = [
            transform_item(item)
            for item in raw_items
        ]

        scored_items = []

        for item in transformed_items:
            score = calculate_score(item, search_terms)

            if score > 0:
                result = dict(item)
                result["score"] = score
                scored_items.append(result)

        scored_items.sort(
            key=lambda current_item: current_item["score"],
            reverse=True
        )

        results = scored_items[:limit]

        return jsonify({
            "query": query,
            "results": results,
            "resultCount": len(results)
        })

    except requests.HTTPError as error:
        status_code = (
            error.response.status_code
            if error.response is not None
            else 502
        )
        response_text = (
            error.response.text
            if error.response is not None
            else str(error)
        )
        return jsonify({
            "error": "TOPdesk request failed",
            "statusCode": status_code,
            "message": response_text
        }), status_code

    except requests.RequestException as error:
        return jsonify({
            "error": "TOPdesk connection failed",
            "message": str(error)
        }), 502

    except RuntimeError as error:
        return jsonify({
            "error": "Configuration error",
            "message": str(error)
        }), 500


# ---------------------------------------------------------
# Swagger 2.0 definition for Copilot Studio
# ---------------------------------------------------------

@app.get("/swagger.json")
def swagger():
    return jsonify({
        "swagger": "2.0",
        "info": {
            "title": "TOPdesk Knowledge Base Proxy",
            "description": (
                "Searches and retrieves live TOPdesk Knowledge Base articles."
            ),
            "version": "2.1.0"
        },
        "host": "topdesk-agent-test.onrender.com",
        "basePath": "/",
        "schemes": ["https"],
        "produces": ["application/json"],
        "securityDefinitions": {
            "apiKey": {
                "type": "apiKey",
                "name": "X-API-Key",
                "in": "header"
            }
        },
        "security": [
            {"apiKey": []}
        ],
        "paths": {
            "/search": {
                "get": {
                    "summary": "Search TOPdesk Knowledge Base",
                    "description": (
                        "Searches Knowledge Item numbers, titles, descriptions, "
                        "article content and keywords."
                    ),
                    "operationId": "SearchTopdeskKnowledgeItems",
                    "parameters": [
                        {
                            "name": "q",
                            "in": "query",
                            "description": (
                                "Search words or a Knowledge Item number."
                            ),
                            "required": True,
                            "type": "string"
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "description": "Maximum number of results.",
                            "required": False,
                            "type": "integer",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 25
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Matching TOPdesk Knowledge Items.",
                            "schema": {
                                "$ref": "#/definitions/SearchResponse"
                            }
                        },
                        "400": {
                            "description": "Search query is missing."
                        },
                        "401": {
                            "description": "Invalid API key."
                        }
                    }
                }
            },
            "/knowledge-items/{identifier}": {
                "get": {
                    "summary": "Get one TOPdesk Knowledge Item",
                    "description": (
                        "Retrieves one Knowledge Item using its UUID or KI number."
                    ),
                    "operationId": "GetTopdeskKnowledgeItem",
                    "parameters": [
                        {
                            "name": "identifier",
                            "in": "path",
                            "description": (
                                "Knowledge Item UUID or KI number, "
                                "for example KI 0080."
                            ),
                            "required": True,
                            "type": "string"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Requested TOPdesk Knowledge Item.",
                            "schema": {
                                "$ref": "#/definitions/KnowledgeItem"
                            }
                        },
                        "401": {
                            "description": "Invalid API key."
                        },
                        "404": {
                            "description": "Knowledge Item not found."
                        }
                    }
                }
            }
        },
        "definitions": {
            "KnowledgeItem": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "number": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "content": {"type": "string"},
                    "keywords": {"type": "string"},
                    "modificationDate": {"type": "string"},
                    "score": {"type": "integer"}
                }
            },
            "SearchResponse": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "resultCount": {"type": "integer"},
                    "results": {
                        "type": "array",
                        "items": {
                            "$ref": "#/definitions/KnowledgeItem"
                        }
                    }
                }
            }
        }
    })


# ---------------------------------------------------------
# Error handlers
# ---------------------------------------------------------

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "error": "Not found",
        "availableEndpoints": [
            "/health",
            "/search?q=helpdesk",
            "/knowledge-items?page_size=10",
            "/knowledge-items/KI%200080",
            "/swagger.json"
        ]
    }), 404


# ---------------------------------------------------------
# Local development
# ---------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port
    )
