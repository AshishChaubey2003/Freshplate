"""
Gemini integration for the FreshPlate assistant.

Kept out of views.py so it can be mocked in tests and swapped for another
provider without touching the API layer.
"""
import logging
from functools import lru_cache

from django.conf import settings

from food.models import FoodItem
from orders.models import Order

logger = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = """You are FreshPlate AI Assistant, a friendly helper for FreshPlate —
a cloud kitchen and food rescue platform.

You help users with: recommending dishes from the menu, explaining how to place
and track orders, the food donation process, and general food questions.

Rules:
- Only recommend dishes that appear in the MENU below. Never invent dishes or prices.
- For order status, use only the RECENT ORDERS below. If an order is not listed, say you cannot see it.
- You cannot place, change or cancel orders yourself; point the user to the right page
  (Menu, Cart, My Orders, Donate).
- Do not give medical advice; for allergies suggest checking with a professional.
- Keep answers short and clear. Reply in the same language the user writes in.
"""


class ChatbotUnavailable(Exception):
    """The AI provider is not configured or the request failed."""


class ChatbotSafetyBlocked(Exception):
    """The model returned no text (usually a safety filter)."""


@lru_cache(maxsize=1)
def _get_client():
    """Built on first use, not at import time.

    Creating it at import time meant a missing API key crashed the whole Django
    process instead of just the chat endpoint.
    """
    if not settings.GEMINI_API_KEY:
        raise ChatbotUnavailable("GEMINI_API_KEY is not configured")

    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=settings.GEMINI_TIMEOUT_MS),
    )


def build_context(user, menu_limit=40, orders_limit=5):
    """Ground the model in real data so it stops inventing dishes and order status."""
    items = (
        FoodItem.objects.select_related("category")
        .filter(is_available=True, category__is_active=True)
        .order_by("-total_orders")[:menu_limit]
    )
    menu_lines = [
        f"- {i.name} | {i.category.name} | {'Veg' if i.is_veg else 'Non-veg'} | Rs {i.final_price}"
        + (f" | {i.calories} kcal" if i.calories else "")
        + (" | Food rescue deal" if i.status == "rescue" else "")
        for i in items
    ]

    orders = Order.objects.filter(user=user).order_by("-ordered_at")[:orders_limit]
    order_lines = [
        f"- Order #{o.id}: {o.get_status_display()}, Rs {o.total_amount}, placed {o.ordered_at:%d %b %Y %H:%M}"
        for o in orders
    ]

    first_name = (user.full_name or "").split(" ")[0]
    return (
        f"USER: {first_name} (role: {user.role})\n\n"
        "MENU:\n" + ("\n".join(menu_lines) or "- (menu is currently empty)") + "\n\n"
        "RECENT ORDERS:\n" + ("\n".join(order_lines) or "- (no orders yet)")
    )


def generate_reply(user, history):
    """
    history: list of (role, content) tuples, oldest first, ending with the new
    user message. Returns the assistant's reply text.
    """
    from google.genai import errors, types

    client = _get_client()

    contents = [
        types.Content(role="user" if role == "user" else "model", parts=[types.Part(text=content)])
        for role, content in history
    ]
    system_instruction = f"{BASE_SYSTEM_PROMPT}\n{build_context(user)}"

    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.6,
                max_output_tokens=settings.GEMINI_MAX_OUTPUT_TOKENS,
            ),
        )
    except errors.APIError as exc:
        logger.exception("Gemini API error (model=%s)", settings.GEMINI_MODEL)
        raise ChatbotUnavailable(str(exc)) from exc
    except Exception as exc:  # timeouts, DNS, anything else
        logger.exception("Unexpected error calling Gemini")
        raise ChatbotUnavailable(str(exc)) from exc

    candidate = response.candidates[0] if response and response.candidates else None
    if candidate is not None and str(getattr(candidate, "finish_reason", "")).endswith("MAX_TOKENS"):
        logger.warning("Gemini reply hit max_output_tokens=%s (model=%s); it may be cut off",
                       settings.GEMINI_MAX_OUTPUT_TOKENS, settings.GEMINI_MODEL)
    text = (response.text or "").strip() if response else ""
    if not text:
        raise ChatbotSafetyBlocked()
    return text