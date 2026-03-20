from __future__ import annotations
import contextlib
import logging
import os
from typing import Any, Optional

from dotenv import load_dotenv
load_dotenv("src/.env")

from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

# Custom handler for GET /mcp to return tool list for discovery
async def get_tools_handler(request):
    try:
        tools = await mcp.list_tools()
        return JSONResponse({"tools": [tool.model_dump() for tool in tools]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
import uvicorn

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise ImportError(
        "The 'mcp' package is required. Install it with: pip install mcp"
    ) from exc

from mcp.server.fastmcp.server import Context
from src.helpers import (
    DataFileError,
    find_product_by_sku,
    find_store_by_id,
    get_next_id,
    load_inventory,
    load_products,
    load_stores,
    save_inventory,
    save_products,
)
from src.middleware import require_api_key

# Patch transport security BEFORE creating FastMCP to allow ngrok hosts
import os
os.environ["MCP_DISABLE_HOST_CHECK"] = "1"

try:
    from mcp.server import transport_security
    original_validate = transport_security.validate_host
    def patched_validate(host: str, allowed_hosts: list = None):
        return True  # Allow all hosts for ngrok compatibility
    transport_security.validate_host = patched_validate
except (ImportError, AttributeError):
    pass  # If the structure is different, skip patching

class HostHeaderFixMiddleware(BaseHTTPMiddleware):
    """Fix Host header for ngrok compatibility."""
    async def dispatch(self, request, call_next):
        # Replace ngrok host with localhost to pass MCP security check
        if '.ngrok' in request.headers.get('host', ''):
            # Create a mutable copy of headers
            from starlette.datastructures import MutableHeaders
            scope = request.scope
            headers = MutableHeaders(scope=scope)
            del headers['host']
            headers['host'] = 'localhost:8000'
        return await call_next(request)

class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.logger = logging.getLogger("mcp.server")
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

    async def dispatch(self, request, call_next):
        # Skip logging for common security probe paths
        skip_paths = ['/.aws/', '/.ada/', '/.ssh/', '/.midway/', '/.env', '/wp-admin', '/admin']
        if any(skip in str(request.url.path) for skip in skip_paths):
            return await call_next(request)
        
        self.logger.info("MCP request: %s %s", request.method, request.url)
        self.logger.debug("Headers: %s", dict(request.headers))
        try:
            response = await call_next(request)
        except Exception as exc:
            self.logger.exception("MCP request failed")
            raise
        self.logger.info("MCP response: %s %s -> %s", request.method, request.url, response.status_code)
        return response


mcp = FastMCP(
    "zava-inventory-server",
    json_response=True,
    stateless_http=True,
)

# Note: Copilot Studio sends JSON-RPC 2.0 calls directly; stateless JSON handling
# ensures immediate replies instead of SSE stream protocol


# -----------------------------
# Pydantic schemas
# -----------------------------
class Product(BaseModel):
    productId: int = Field(..., description="Unique numeric product identifier")
    sku: str = Field(..., description="Stock keeping unit, e.g. WBH-001")
    name: str = Field(..., description="Display product name")
    category: str = Field(..., description="Business category")
    description: str = Field(..., description="Human-friendly product description")
    price: float = Field(..., ge=0, description="Unit price in USD")


class NewProductInput(BaseModel):
    sku: str = Field(..., description="Unique SKU for the new product")
    name: str = Field(..., description="Display product name")
    category: str = Field(..., description="Business category")
    description: str = Field(..., description="Human-friendly product description")
    price: float = Field(..., ge=0, description="Unit price in USD")
    initialQuantityByStore: dict[int, int] = Field(
        default_factory=dict,
        description="Optional map of storeId -> starting inventory quantity",
    )
    reorderLevel: int = Field(
        10,
        ge=0,
        description="Threshold that indicates when the item should be reordered",
    )


class Store(BaseModel):
    id: int = Field(..., description="Unique numeric store identifier")
    name: str = Field(..., description="Store display name")
    address: str = Field(..., description="Store street address")
    city: str = Field(..., description="Store city")
    country: str = Field(..., description="Store country")


class InventoryItem(BaseModel):
    id: int = Field(..., description="Unique inventory row identifier")
    storeId: int = Field(..., description="Store identifier")
    productId: int = Field(..., description="Product identifier")
    sku: str = Field(..., description="Product SKU")
    productName: str = Field(..., description="Product name copy for quick display")
    productCategory: str = Field(..., description="Product category copy for quick display")
    productDescription: str = Field(..., description="Product description copy for quick display")
    price: float = Field(..., ge=0, description="Unit price in USD")
    quantity: int = Field(..., ge=0, description="On-hand quantity in that store")
    reorderLevel: int = Field(..., ge=0, description="Reorder threshold")
    inStock: bool = Field(..., description="Whether quantity is greater than zero")


class InventoryAdjustmentInput(BaseModel):
    storeId: int = Field(..., description="Store identifier")
    sku: str = Field(..., description="Product SKU to update")
    quantity: int = Field(..., ge=0, description="New on-hand quantity")
    reorderLevel: Optional[int] = Field(
        None,
        ge=0,
        description="Optional new reorder level for this store-product row",
    )



# -----------------------------
# MCP tools
# -----------------------------
@mcp.tool()
@require_api_key
def get_products(
    category: Optional[str] = None,
    sku: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    ctx: Context | None = None,
) -> list[Product]:
    """Return products, optionally filtered by category, SKU, or free-text search."""
    products = load_products()

    if category:
        products = [p for p in products if p["category"].lower() == category.strip().lower()]
    if sku:
        products = [p for p in products if p["sku"].lower() == sku.strip().lower()]
    if search:
        q = search.strip().lower()
        products = [
            p for p in products
            if q in p["name"].lower()
            or q in p["description"].lower()
            or q in p["category"].lower()
            or q in p["sku"].lower()
        ]

    limit = max(1, min(limit, 200))
    return [Product(**p) for p in products[:limit]]


@mcp.tool()
@require_api_key
def get_product_by_sku(sku: str, ctx: Context | None = None) -> Product:
    """Return one product by SKU."""
    product = find_product_by_sku(sku)
    if not product:
        raise ValueError(f"No product found for SKU '{sku}'.")
    return Product(**product)


@mcp.tool()
@require_api_key
def add_product(payload: NewProductInput, ctx: Context | None = None) -> dict[str, Any]:
    """Add a new product and optionally seed inventory rows by store."""
    products = load_products()
    inventory = load_inventory()
    stores = load_stores()

    if any(p["sku"].strip().lower() == payload.sku.strip().lower() for p in products):
        raise ValueError(f"SKU '{payload.sku}' already exists.")

    new_product = Product(
        productId=get_next_id(products, "productId"),
        sku=payload.sku,
        name=payload.name,
        category=payload.category,
        description=payload.description,
        price=payload.price,
    )
    products.append(new_product.model_dump())
    save_products(products)

    next_inventory_id = get_next_id(inventory, "id")
    seeded_rows = []
    valid_store_ids = {int(s["id"]) for s in stores}

    for store_id, qty in payload.initialQuantityByStore.items():
        if int(store_id) not in valid_store_ids:
            raise ValueError(f"Store id '{store_id}' does not exist.")

        row = InventoryItem(
            id=next_inventory_id,
            storeId=int(store_id),
            productId=new_product.productId,
            sku=new_product.sku,
            productName=new_product.name,
            productCategory=new_product.category,
            productDescription=new_product.description,
            price=new_product.price,
            quantity=int(qty),
            reorderLevel=payload.reorderLevel,
            inStock=int(qty) > 0,
        )
        seeded_rows.append(row.model_dump())
        next_inventory_id += 1

    if seeded_rows:
        inventory.extend(seeded_rows)
        save_inventory(inventory)

    return {
        "message": "Product added successfully.",
        "product": new_product.model_dump(),
        "seededInventoryRows": seeded_rows,
    }


@mcp.tool()
@require_api_key
def get_stores(ctx: Context | None = None) -> list[Store]:
    """Return all stores."""
    return [Store(**s) for s in load_stores()]


@mcp.tool()
@require_api_key
def list_inventory_by_store(
    store_id: Optional[int] = None,
    store_name: Optional[str] = None,
    low_stock_only: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return inventory rows for a store, resolved by store_id or store_name."""
    stores = load_stores()
    inventory = load_inventory()

    if store_id is None and not store_name:
        raise ValueError("Provide either store_id or store_name.")

    store = None
    if store_id is not None:
        store = next((s for s in stores if int(s["id"]) == int(store_id)), None)
    elif store_name:
        store = next((s for s in stores if s["name"].strip().lower() == store_name.strip().lower()), None)

    if not store:
        raise ValueError("Store not found.")

    rows = [r for r in inventory if int(r["storeId"]) == int(store["id"])]
    if low_stock_only:
        rows = [r for r in rows if int(r["quantity"]) <= int(r.get("reorderLevel", 0))]

    rows.sort(key=lambda r: (r["productCategory"], r["productName"]))
    return {
        "store": store,
        "itemCount": len(rows),
        "items": [InventoryItem(**r).model_dump() for r in rows],
    }


@mcp.tool()
@require_api_key
def update_inventory(payload: InventoryAdjustmentInput, ctx: Context | None = None) -> dict[str, Any]:
    """Update quantity/reorderLevel for a specific store + SKU inventory row."""
    inventory = load_inventory()

    target = next(
        (
            r for r in inventory
            if int(r["storeId"]) == int(payload.storeId)
            and r["sku"].strip().lower() == payload.sku.strip().lower()
        ),
        None,
    )

    if not target:
        raise ValueError(
            f"No inventory row found for storeId={payload.storeId} and sku='{payload.sku}'."
        )

    target["quantity"] = int(payload.quantity)
    target["inStock"] = int(payload.quantity) > 0
    if payload.reorderLevel is not None:
        target["reorderLevel"] = int(payload.reorderLevel)

    save_inventory(inventory)

    return {
        "message": "Inventory updated successfully.",
        "inventoryItem": InventoryItem(**target).model_dump(),
    }


@mcp.tool()
@require_api_key
def get_inventory_summary(ctx: Context | None = None) -> dict[str, Any]:
    """Return high-level counts for quick dashboarding/testing."""
    products = load_products()
    stores = load_stores()
    inventory = load_inventory()

    total_units = sum(int(row["quantity"]) for row in inventory)
    low_stock_rows = [row for row in inventory if int(row["quantity"]) <= int(row.get("reorderLevel", 0))]

    return {
        "productCount": len(products),
        "storeCount": len(stores),
        "inventoryRowCount": len(inventory),
        "totalUnits": total_units,
        "lowStockRowCount": len(low_stock_rows),
    }

@mcp.tool()
@require_api_key
def get_store_by_id(store_id: int, ctx: Context | None = None) -> dict[str, Any]:
    """Return one store by its identifier."""
    store = find_store_by_id(store_id)
    if not store:
        raise ValueError(f"No store found for id '{store_id}'.")
    return store

@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield

mcp_app = mcp.streamable_http_app()

# Wrap the MCP app with middleware to fix ngrok host headers
app = Starlette(
    routes=[
        Route("/tools", get_tools_handler, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    middleware=[
        Middleware(HostHeaderFixMiddleware),
        Middleware(LoggingMiddleware),
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except DataFileError as exc:
        raise SystemExit(f"Data file error: {exc}") from exc