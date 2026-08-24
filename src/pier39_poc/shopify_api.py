"""Shopify Admin GraphQL client.

Two calls per store:

1. `metafieldDefinitions(ownerType: PRODUCT)` -- diagnostic only. The definition list
   both misses undefined namespaces (skout has values under `global`, `stamped`,
   `okendo`, `SEOMetaManager`, `msft_bingads` with no definitions) and overstates
   coverage for defined-but-empty keys (remi defines the whole `agentiq` namespace and
   leaves it blank). Never treat it as authoritative.

2. Paginated `products` with metafields and variants nested inline, which avoids an
   N+1 and keeps the whole catalogue to one or two calls. skout's 182 products fit in
   a single page.

`onlineStoreUrl` is both the URL source and the publication filter: it is null when the
product is not published to the online store channel. No URL is ever built by string
concatenation.

Price and inventory are deliberately NOT selected here. They are read live at query
time via `fetch_live_variants`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx

from .config import StoreConfig

PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      handle
      title
      vendor
      productType
      tags
      status
      publishedAt
      createdAt
      updatedAt
      onlineStoreUrl
      descriptionHtml
      templateSuffix
      seo { title description }
      collections(first: 50) { nodes { id handle title } }
      metafields(first: 250) {
        nodes { namespace key type value updatedAt createdAt }
      }
      variants(first: 100) {
        nodes {
          id
          title
          sku
          selectedOptions { name value }
        }
      }
    }
  }
}
"""

# templateSuffix is unverified against a live schema (see DESIGN.md 10). This variant
# drops it so a schema error does not block the whole fetch.
PRODUCTS_QUERY_NO_TEMPLATE = PRODUCTS_QUERY.replace("      templateSuffix\n", "")

DEFINITIONS_QUERY = """
query Definitions($cursor: String) {
  metafieldDefinitions(ownerType: PRODUCT, first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id name namespace key description type { name } }
  }
}
"""

LIVE_VARIANTS_QUERY = """
query LiveVariants($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      title
      sku
      availableForSale
      inventoryQuantity
      price
      compareAtPrice
      product { id handle title onlineStoreUrl }
    }
  }
}
"""


class ShopifyError(RuntimeError):
    pass


@dataclass
class Throttle:
    """Observed cost from the last response, for logging and adaptive backoff."""

    requested: int = 0
    actual: int = 0
    available: float = 0.0
    restore_rate: float = 0.0

    @classmethod
    def from_extensions(cls, ext: dict[str, Any] | None) -> "Throttle":
        cost = (ext or {}).get("cost") or {}
        status = cost.get("throttleStatus") or {}
        return cls(
            requested=cost.get("requestedQueryCost", 0),
            actual=cost.get("actualQueryCost", 0),
            available=status.get("currentlyAvailable", 0.0),
            restore_rate=status.get("restoreRate", 0.0),
        )


class AdminClient:
    def __init__(self, store: StoreConfig, token: str, timeout: float = 60.0):
        self.store = store
        self.url = store.graphql_url()
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self.last_throttle = Throttle()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AdminClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def execute(
        self, query: str, variables: dict[str, Any] | None = None, attempt: int = 0
    ) -> dict[str, Any]:
        response = self._client.post(
            self.url, json={"query": query, "variables": variables or {}}
        )

        if response.status_code == 401:
            raise ShopifyError(
                f"{self.store.slug}: 401 Unauthorized. The token is wrong, revoked, or "
                "belongs to another shop."
            )
        if response.status_code == 402:
            raise ShopifyError(f"{self.store.slug}: 402 -- shop is frozen or unavailable.")
        if response.status_code == 404:
            raise ShopifyError(
                f"{self.store.slug}: 404 at {self.url}. Check the domain and that "
                f"admin_api_version '{self.store.admin_api_version}' exists."
            )
        if response.status_code == 429 and attempt < 5:
            wait = float(response.headers.get("Retry-After", 2 ** attempt))
            time.sleep(wait)
            return self.execute(query, variables, attempt + 1)
        response.raise_for_status()

        payload = response.json()
        self.last_throttle = Throttle.from_extensions(payload.get("extensions"))

        errors = payload.get("errors")
        if errors:
            messages = "; ".join(e.get("message", str(e)) for e in errors)
            throttled = any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors
            )
            if throttled and attempt < 5:
                time.sleep(2 ** attempt)
                return self.execute(query, variables, attempt + 1)
            if "access denied" in messages.lower() or "scope" in messages.lower():
                raise ShopifyError(
                    f"{self.store.slug}: {messages}. The token likely lacks read_products."
                )
            raise ShopifyError(f"{self.store.slug}: GraphQL errors: {messages}")

        data = payload.get("data")
        if data is None:
            raise ShopifyError(f"{self.store.slug}: response had no data block")
        return data

    # ----------------------------------------------------------------- #

    def metafield_definitions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self.execute(DEFINITIONS_QUERY, {"cursor": cursor})
            block = data["metafieldDefinitions"]
            out.extend(block["nodes"])
            if not block["pageInfo"]["hasNextPage"]:
                return out
            cursor = block["pageInfo"]["endCursor"]

    def products(self) -> Iterator[dict[str, Any]]:
        """Yield every active product. Falls back if templateSuffix is unsupported."""
        query = PRODUCTS_QUERY
        cursor: str | None = None
        while True:
            try:
                data = self.execute(query, {"cursor": cursor})
            except ShopifyError as exc:
                if "templateSuffix" in str(exc) and query is PRODUCTS_QUERY:
                    query = PRODUCTS_QUERY_NO_TEMPLATE
                    continue
                raise
            block = data["products"]
            for node in block["nodes"]:
                yield flatten_product(node)
            if not block["pageInfo"]["hasNextPage"]:
                return
            cursor = block["pageInfo"]["endCursor"]

    def fetch_live_variants(self, variant_gids: list[str]) -> dict[str, dict[str, Any]]:
        """Live price, inventory and availability. The only place these are read."""
        if not variant_gids:
            return {}
        data = self.execute(LIVE_VARIANTS_QUERY, {"ids": variant_gids})
        out: dict[str, dict[str, Any]] = {}
        for node in data.get("nodes") or []:
            if node and node.get("id"):
                out[node["id"]] = node
        return out


def flatten_product(node: dict[str, Any]) -> dict[str, Any]:
    """Collapse GraphQL connection wrappers into plain lists."""
    return {
        "id": node["id"],
        "product_id": str(node["id"]).rsplit("/", 1)[-1],
        "handle": node.get("handle"),
        "title": node.get("title"),
        "vendor": node.get("vendor"),
        "product_type": node.get("productType"),
        "tags": node.get("tags") or [],
        "status": node.get("status"),
        "published_at": node.get("publishedAt"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "online_store_url": node.get("onlineStoreUrl"),
        "description_html": node.get("descriptionHtml") or "",
        "template_suffix": node.get("templateSuffix"),
        "seo": node.get("seo") or {},
        "collections": (node.get("collections") or {}).get("nodes") or [],
        "metafields": (node.get("metafields") or {}).get("nodes") or [],
        "variants": (node.get("variants") or {}).get("nodes") or [],
    }
