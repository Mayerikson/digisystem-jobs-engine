"""Rode UMA vez no seu computador para obter LINKEDIN_TOKEN e LINKEDIN_PERSON_ID.
Uso: LINKEDIN_CLIENT_ID=... LINKEDIN_CLIENT_SECRET=... python linkedin_auth.py
Pré-requisito: no app do LinkedIn, adicione o redirect http://localhost:8000/callback
e os produtos "Share on LinkedIn" e "Sign In with LinkedIn using OpenID Connect".
"""
import os
from urllib.parse import urlencode

import requests

CID, SEC = os.environ["LINKEDIN_CLIENT_ID"], os.environ["LINKEDIN_CLIENT_SECRET"]
REDIRECT = "http://localhost:8000/callback"

print("1) Abra este link, autorize e copie o valor de 'code=' da URL final (a página dará erro, é normal):\n")
print("https://www.linkedin.com/oauth/v2/authorization?" + urlencode({
    "response_type": "code", "client_id": CID, "redirect_uri": REDIRECT,
    "scope": "openid profile w_member_social", "state": "digisystem",
}))
code = input("\n2) Cole o code aqui: ").strip()

t = requests.post("https://www.linkedin.com/oauth/v2/accessToken", data={
    "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
    "client_id": CID, "client_secret": SEC,
}, timeout=60).json()
token = t["access_token"]
pid = requests.get("https://api.linkedin.com/v2/userinfo",
                   headers={"Authorization": f"Bearer {token}"}, timeout=60).json()["sub"]

print(f"\nLINKEDIN_TOKEN={token}\nLINKEDIN_PERSON_ID={pid}")
print(f"(token vale ~{t.get('expires_in', 0) // 86400} dias)")
