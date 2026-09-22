from flask import Flask, jsonify
import requests
import os

app = Flask(__name__)

TOPDESK_USER = os.getenv("TOPDESK_USER")
TOPDESK_TOKEN = os.getenv("TOPDESK_TOKEN")

@app.route("/")
def knowledge():

    response = requests.get(
        "https://saether.topdesk.net/services/knowledge-base-v1/knowledgeItems?page_size=100",
        auth=(TOPDESK_USER, TOPDESK_TOKEN)
    )

    return jsonify(response.json())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)