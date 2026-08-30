"""Camada de enriquecimento de conteúdo.

O MVP funciona sem chave de catálogo, mas quando TMDB_API_KEY está configurada
esta camada substitui os campos factuais sensíveis por dados obtidos de uma fonte
externa. O modelo não é tratado como banco de dados.
"""

import json
import os
import urllib.parse
import urllib.request

IMAGE_BASE = "https://image.tmdb.org/t/p/w780"


class ContentMetadataProvider:
    def lookup(self, title_original: str, title_pt: str = "", year: int = 0):
        raise NotImplementedError


class NullMetadataProvider(ContentMetadataProvider):
    def lookup(self, title_original: str, title_pt: str = "", year: int = 0):
        return {
            "images": {"poster": "", "backdrop": ""},
            "availability": [],
            "metadata_source": "Fonte externa não configurada",
            "availability_verified": False,
        }


class TMDBMetadataProvider(ContentMetadataProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, path: str, params: dict):
        query = {"api_key": self.api_key, **params}
        url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def lookup(self, title_original: str, title_pt: str = "", year: int = 0):
        query = title_original or title_pt
        if not query:
            return NullMetadataProvider().lookup(title_original, title_pt, year)
        params = {"query": query, "language": "pt-BR", "include_adult": "false"}
        if year:
            params["year"] = year
        data = self._get("/search/movie", params)
        results = data.get("results") or []
        if not results and title_pt and title_pt != title_original:
            data = self._get("/search/movie", {"query": title_pt, "language": "pt-BR", "include_adult": "false"})
            results = data.get("results") or []
        if not results:
            return NullMetadataProvider().lookup(title_original, title_pt, year)

        movie = results[0]
        movie_id = movie.get("id")
        details = self._get(f"/movie/{movie_id}", {"language": "pt-BR", "append_to_response": "watch/providers,external_ids"})
        providers = (details.get("watch/providers") or {}).get("results", {}).get("BR", {})
        availability = []
        for item in providers.get("flatrate", []) or []:
            if item.get("provider_name"):
                availability.append({"platform": item["provider_name"], "type": "subscription", "url": providers.get("link", "")})
        for item in providers.get("rent", []) or []:
            if item.get("provider_name"):
                availability.append({"platform": item["provider_name"], "type": "rent", "url": providers.get("link", "")})
        for item in providers.get("buy", []) or []:
            if item.get("provider_name"):
                availability.append({"platform": item["provider_name"], "type": "buy", "url": providers.get("link", "")})

        genres = [genre.get("name", "") for genre in details.get("genres", []) if genre.get("name")]
        return {
            "images": {
                "poster": (IMAGE_BASE + movie["poster_path"]) if movie.get("poster_path") else "",
                "backdrop": (IMAGE_BASE + movie["backdrop_path"]) if movie.get("backdrop_path") else "",
            },
            "title_original": details.get("original_title") or title_original,
            "title_pt": details.get("title") or title_pt,
            "year": int((details.get("release_date") or "0")[:4] or 0),
            "runtime_minutes": int(details.get("runtime") or 0),
            "genres": genres[:3],
            "synopsis": details.get("overview") or "",
            "availability": availability[:6],
            "metadata_source": "TMDB",
            "availability_verified": bool(availability),
        }


def get_metadata_provider() -> ContentMetadataProvider:
    key = os.environ.get("TMDB_API_KEY", "").strip()
    return TMDBMetadataProvider(key) if key else NullMetadataProvider()


def enrich_result(result: dict) -> dict:
    provider = get_metadata_provider()
    for recommendation in result.get("recommendations", []):
        try:
            metadata = provider.lookup(
                recommendation.get("title_original", ""),
                recommendation.get("title_pt", ""),
                recommendation.get("year", 0),
            )
        except Exception:
            metadata = NullMetadataProvider().lookup(
                recommendation.get("title_original", ""), recommendation.get("title_pt", ""), recommendation.get("year", 0)
            )
        recommendation["poster_url"] = metadata["images"].get("poster", "")
        recommendation["backdrop_url"] = metadata["images"].get("backdrop", "")
        recommendation["metadata_source"] = metadata.get("metadata_source", "")
        recommendation["availability_verified"] = metadata.get("availability_verified", False)
        recommendation["availability_note"] = "" if metadata.get("availability_verified") else "Disponibilidade não confirmada no momento."
        if metadata.get("availability"):
            recommendation["where_to_watch"] = [
                {"platform": item["platform"], "type": item["type"]} for item in metadata["availability"] if item.get("platform")
            ]
        else:
            recommendation["where_to_watch"] = []
        for key in ("title_original", "title_pt", "year", "runtime_minutes", "genres", "synopsis"):
            value = metadata.get(key)
            if value not in (None, "", 0, []):
                recommendation[key] = value
    return result
