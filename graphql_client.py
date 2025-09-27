"""GraphQL client for interacting with Avtor24 API."""

import asyncio
import json
from typing import Dict, Any, Optional, List
from urllib.parse import urljoin

from playwright.async_api import APIRequestContext, Page


class GraphQLClient:
    """Client for making GraphQL requests to Avtor24 API."""

    def __init__(self, page: Page):
        self.page = page
        self.api_url = "https://avtor24.ru/graphqlapi"
        # Cache for customer IDs to avoid repeated API calls
        self._customer_id_cache: Dict[str, str] = {}
        # Rate limiting
        self._last_request_time = 0
        self._min_request_interval = 0.1  # 100ms between requests
        # Statistics
        self._request_count = 0
        self._cache_hits = 0

    async def _get_request_context(self) -> APIRequestContext:
        """Get API request context from the page."""
        # Use the browser context's request to maintain cookies and session
        return self.page.context.request

    async def _rate_limit(self):
        """Implement rate limiting to avoid overwhelming the API."""
        import time
        current_time = time.time()
        time_since_last = current_time - self._last_request_time

        if time_since_last < self._min_request_interval:
            await asyncio.sleep(self._min_request_interval - time_since_last)

        self._last_request_time = time.time()

    async def get_customer_id(self, order_id: str) -> Optional[str]:
        """Get customer ID for an order with caching and rate limiting."""
        # Check cache first
        if order_id in self._customer_id_cache:
            self._cache_hits += 1
            return self._customer_id_cache[order_id]

        # Apply rate limiting
        await self._rate_limit()

        try:
            # Get minimal order info for customer ID
            order_info = await self._get_minimal_order_info(order_id)

            # Check if order_info is None or empty
            if order_info is None:
                print(f"Warning: order_info is None for order {order_id}")
                return None

            # Extract customer info
            data = order_info.get("data")
            if data is None:
                print(f"Warning: data is None in order_info for order {order_id}")
                return None

            dialog = data.get("dialog")
            if dialog is None:
                print(f"Warning: dialog is None in order_info for order {order_id} - order may not exist or be inaccessible")
                return None

            customer = dialog.get("customer", {})

            # Try different possible customer ID fields
            customer_id = (
                customer.get("id") or
                customer.get("nickName") or
                customer.get("userId")
            )

            if customer_id:
                # Cache the result
                self._customer_id_cache[order_id] = str(customer_id)
                return str(customer_id)
            else:
                print(f"Warning: No customer_id found in response for order {order_id}")

        except Exception as exc:
            print(f"Warning: Failed to get customer_id for order {order_id}: {exc}")
            import traceback
            traceback.print_exc()

        return None

    async def get_customer_ids_batch(self, order_ids: List[str]) -> Dict[str, Optional[str]]:
        """Get customer IDs for multiple orders with caching and batching."""
        result = {}
        uncached_orders = []

        # Check cache first
        for order_id in order_ids:
            if order_id in self._customer_id_cache:
                result[order_id] = self._customer_id_cache[order_id]
                self._cache_hits += 1
            else:
                uncached_orders.append(order_id)

        if not uncached_orders:
            return result

        # For batch requests, use individual requests with rate limiting
        # (since the GraphQL schema doesn't seem to support true batching)
        for order_id in uncached_orders:
            try:
                customer_id = await self.get_customer_id(order_id)
                result[order_id] = customer_id
            except Exception as exc:
                print(f"Warning: Failed to get customer_id for order {order_id} in batch: {exc}")
                result[order_id] = None

        return result

    async def _get_minimal_order_info(self, order_id: str) -> Dict[str, Any]:
        """Get minimal order information for customer ID only."""
        query = """
        query getOrderCustomer($orderId: ID!) {
          dialog(orderId: $orderId) {
            id
            customer {
              id
              nickName
              __typename
            }
            __typename
          }
        }
        """

        variables = {"orderId": order_id}
        return await self.execute_query(query, variables, "getOrderCustomer")

    def clear_customer_id_cache(self):
        """Clear the customer ID cache."""
        self._customer_id_cache.clear()

    def get_cache_stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        cache_hit_rate = 0
        if self._request_count > 0:
            cache_hit_rate = int((self._cache_hits / self._request_count) * 100)

        return {
            "cached_customer_ids": len(self._customer_id_cache),
            "total_requests": self._request_count,
            "cache_hits": self._cache_hits,
            "cache_hit_rate_percent": cache_hit_rate
        }

    async def execute_query(self, query: str, variables: Optional[Dict[str, Any]] = None, operation_name: Optional[str] = None) -> Dict[str, Any]:
        """Execute a GraphQL query or mutation."""
        self._request_count += 1
        request_context = await self._get_request_context()

        payload = {
            "query": query
        }

        if variables:
            payload["variables"] = variables

        if operation_name:
            payload["operationName"] = operation_name

        response = await request_context.post(
            self.api_url,
            data=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://avtor24.ru",
                "Referer": "https://avtor24.ru/home/chat"
            }
        )

        if response.status != 200:
            error_text = await response.text()
            print(f"❌ Ошибка HTTP {response.status}: {error_text}")
            raise Exception(f"GraphQL request failed with status {response.status}: {error_text}")

        result = await response.json()
        # Ensure we always return a dict, never None
        if result is None:
            result = {"data": None, "errors": ["Empty response from server"]}
        return result

    async def send_message(self, order_id: str, text: str) -> Dict[str, Any]:
        """Send a message to an order chat via GraphQL API or browser interface."""
        print(f"💬 Отправка сообщения в заказ {order_id}: {text[:50]}...")

        # Try GraphQL API first (faster and more reliable)
        try:
            result = await self._send_message_via_graphql(order_id, text)
            if result and result.get("data", {}).get("addComment"):
                print("✅ Сообщение отправлено через GraphQL API")
                return result
            else:
                print(f"⚠️ GraphQL API вернул некорректный ответ, пробую браузер")
        except Exception as graphql_exc:
            print(f"⚠️ GraphQL API недоступен ({type(graphql_exc).__name__}), пробую браузер")

        # Fallback to browser interface
        try:
            browser_result = await self._send_message_via_browser(order_id, text)
            if browser_result:
                print("✅ Сообщение отправлено через интерфейс браузера")
                return {"data": {"addComment": {"id": "browser_interface_sent", "text": text}}}
        except Exception as browser_exc:
            print(f"❌ Ошибка отправки через браузер: {browser_exc}")

        # If both methods fail, return error
        raise Exception("Не удалось отправить сообщение ни одним из способов")

    async def _send_message_via_graphql(self, order_id: str, text: str) -> Dict[str, Any]:
        """Send message directly via GraphQL API (fastest method)."""
        mutation = """
        mutation addComment($orderId: ID!, $text: String!) {
          addComment(orderId: $orderId, text: $text) {
            __typename
            ...messageFragment
          }
        }

        fragment messageFragment on message {
          id
          user_id
          text
          creation
          isAdminComment
          isAutoHidden
          isRead
          watched
          files {
            id
            name
            hash
            type
            path
            sizeInMb
            isFinal
            __typename
          }
          __typename
        }
        """

        variables = {
            "orderId": order_id,
            "text": text
        }

        return await self.execute_query(mutation, variables, "addComment")

    async def _send_message_via_js(self, order_id: str, text: str) -> Dict[str, Any]:
        """Send message using JavaScript in browser context."""
        # Use a simpler approach - call the existing GraphQL function via JavaScript
        js_code = f"""
        () => {{
            // Try to use the existing GraphQL client if available
            if (window.graphqlClient && window.graphqlClient.mutate) {{
                return window.graphqlClient.mutate({{
                    mutation: `mutation addComment($orderId: ID!, $text: String!) {{
                        addComment(orderId: $orderId, text: $text) {{
                            __typename
                            id
                            user_id
                            text
                            creation
                            isAdminComment
                            isAutoHidden
                            isRead
                            watched
                        }}
                    }}`,
                    variables: {{
                        orderId: '{order_id}',
                        text: {json.dumps(text)}
                    }}
                }});
            }}

            // Fallback: direct fetch
            return fetch('/graphqlapi', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': '*/*',
                    'Origin': 'https://avtor24.ru',
                    'Referer': 'https://avtor24.ru/home/chat'
                }},
                body: JSON.stringify({{
                    operationName: 'addComment',
                    variables: {{
                        orderId: '{order_id}',
                        text: {json.dumps(text)}
                    }},
                    query: `mutation addComment($orderId: ID!, $text: String!) {{
                        addComment(orderId: $orderId, text: $text) {{
                            __typename
                            id
                            user_id
                            text
                            creation
                            isAdminComment
                            isAutoHidden
                            isRead
                            watched
                        }}
                    }}`
                }})
            }})
            .then(response => {{
                if (!response.ok) {{
                    throw new Error(`HTTP ${{response.status}}`);
                }}
                return response.json();
            }})
            .catch(error => {{
                console.error('JavaScript fetch failed:', error);
                throw error;
            }});
        }}
        """

        try:
            result = await self.page.evaluate(js_code)
            return result
        except Exception as exc:
            print(f"❌ JavaScript execution failed: {exc}")
            raise

    async def _send_message_via_browser(self, order_id: str, text: str) -> bool:
        """Send message by opening bid modal and using comment field (like bid workflow)."""
        try:
            # Navigate to order page
            order_url = f"https://avtor24.ru/order/{order_id}"
            await self.page.goto(order_url, wait_until="networkidle")

            # Wait for page to load
            await asyncio.sleep(2)

            # Try different approaches based on order status

            # First, try to open bid modal if available
            from . import auction
            make_offer_button = self.page.locator(auction.MAKE_OFFER_BUTTON).first

            make_offer_count = await make_offer_button.count()
            print(f"📊 Кнопка ставки найдена: {make_offer_count > 0}")

            if make_offer_count > 0:
                # Order is still available for bidding
                print("🎯 Заказ доступен для ставок, открываю модальное окно...")
                await make_offer_button.click()
                await asyncio.sleep(2)  # Wait for modal to open

                # Find modal
                modal = self.page.locator(auction.MODAL).first
                modal_count = await modal.count()
                print(f"📊 Модальное окно найдено: {modal_count > 0}")

                if modal_count > 0:
                    # Find comment input in modal (same as bid workflow)
                    comment_input = modal.locator(auction.COMMENT_INPUT).first
                    comment_count = await comment_input.count()
                    print(f"📊 Поле комментария найдено: {comment_count > 0}")

                    if comment_count > 0:
                        # Fill comment field
                        await comment_input.clear()
                        await comment_input.type(text, delay=100)  # Human-like typing

                        # Find submit button
                        submit_button = self.page.locator(auction.SUBMIT_BUTTON).first
                        submit_count = await submit_button.count()
                        print(f"📊 Кнопка отправки найдена: {submit_count > 0}")

                        if submit_count > 0:
                            await submit_button.click()
                            await asyncio.sleep(3)  # Wait for submission

                            print("✅ Отправил сообщение через модальное окно ставки")
                            return True
                        else:
                            print("❌ Не найдена кнопка отправки в модальном окне")
                    else:
                        print("❌ Не найдено поле комментария в модальном окне")
                else:
                    print("❌ Не открылось модальное окно ставки")
            else:
                # Order might be completed - try chat button
                print("⚠️ Заказ не доступен для ставок, пробую чат...")
                chat_button = self.page.locator("button:has-text('Чат с заказчиком')").first
                chat_count = await chat_button.count()
                print(f"📊 Кнопка чата найдена: {chat_count > 0}")

                if chat_count > 0:
                    await chat_button.click()
                    await asyncio.sleep(3)  # Wait for chat to open

                    # Try to find message input in chat
                    message_inputs = [
                        "textarea[placeholder*='Напишите сообщение']",
                        "textarea[placeholder*='Сообщение']",
                        "div[contenteditable='true']",
                        ".message-input textarea",
                        ".chat-input textarea",
                        "textarea"
                    ]

                    message_input = None
                    for selector in message_inputs:
                        message_input = self.page.locator(selector).first
                        if await message_input.count() > 0:
                            print(f"📊 Найден селектор ввода: {selector}")
                            break

                    if message_input and await message_input.count() > 0:
                        # Type message
                        await message_input.clear()
                        await message_input.type(text, delay=100)

                        # Find send button - try multiple selectors
                        send_buttons = [
                            "button:has-text('Отправить')",
                            "button[type='submit']",
                            ".send-button",
                            "button.send",
                            "button:has-text('Send')",
                            "input[type='submit']",
                            "button[class*='send']",
                            # More specific selectors for chat send button
                            "button[data-testid*='send']",
                            "button[aria-label*='Отправить']",
                            "button[aria-label*='Send']",
                            "button:has(svg)",
                            ".chat-send-button",
                            ".message-send-button",
                            # Avoid generic button selector that might catch wrong button
                            # "button"  # Removed - too generic
                        ]

                        send_button = None
                        found_selector = None

                        for selector in send_buttons:
                            test_button = self.page.locator(selector).first
                            if await test_button.count() > 0:
                                send_button = test_button
                                found_selector = selector
                                break

                        # Additional check: look for button containing the specific send icon SVG
                        if not send_button:
                            # Try to find button with the send icon (plane/paper airplane icon)
                            send_icon_buttons = self.page.locator("button:has(svg path[d*='m2 20.576'])").first
                            if await send_icon_buttons.count() > 0:
                                send_button = send_icon_buttons
                                found_selector = "button:has(send-icon-svg)"
                                print("📊 Найдена кнопка отправки по SVG иконке")

                        # Last resort: find any enabled button near the message input
                        if not send_button:
                            # Look for buttons within the chat container
                            chat_container = self.page.locator(".chat-container, .message-container, .conversation").first
                            if await chat_container.count() > 0:
                                nearby_buttons = chat_container.locator("button:enabled").all()
                                for btn in nearby_buttons:
                                    try:
                                        # Check if button is visible and clickable
                                        if await btn.is_visible() and await btn.is_enabled():
                                            send_button = btn
                                            found_selector = "nearby_enabled_button"
                                            print("📊 Найдена ближайшая активная кнопка в чате")
                                            break
                                    except:
                                        continue

                        if send_button:
                            # Get button text for verification
                            try:
                                button_text = await send_button.text_content()
                                print(f"📊 Найден селектор отправки ({found_selector}): '{button_text.strip()}'")
                            except:
                                print(f"📊 Найден селектор отправки ({found_selector})")

                            await send_button.click()
                            await asyncio.sleep(2)
                            print("✅ Отправил сообщение через чат")
                            return True
                        else:
                            print("❌ Не найдена кнопка отправки в чате")
                    else:
                        print("❌ Не найдено поле ввода в чате")
                else:
                    print("❌ Не найдена кнопка чата")

            return False

        except Exception as exc:
            print(f"❌ Ошибка отправки через браузер: {exc}")
            return False

    async def _verify_message_sent(self, expected_text: str) -> bool:
        """Verify that the message was sent by checking if it appears in chat."""
        try:
            # Look for the message in recent messages
            messages = self.page.locator(".message-content, .message-text, .chat-message")
            count = await messages.count()

            if count > 0:
                # Check last few messages
                for i in range(max(0, count - 3), count):
                    message_text = await messages.nth(i).text_content()
                    if expected_text in message_text:
                        return True

            return False
        except Exception:
            return False

    async def get_order_info(self, order_id: str) -> Dict[str, Any]:
        """Get order information and dialog details."""
        query = """
        query getDialogOrder($orderId: ID!) {
          order(id: $orderId) {
            id
            title
            category {
              id
              name
              __typename
            }
            type {
              id
              name
              __typename
            }
            extendedStage
            isFavorite
            deadline
            customerFiles {
              id
              name
              hash
              path
              type
              sizeInMb
              readableCreationUnixtime
              __typename
            }
            isExpressOrder
            __typename
          }
          dialog(orderId: $orderId) {
            customer {
              nickName
              isOnline
              isWorked
              lastVisit
              isTelegramEnabled
              __typename
            }
            __typename
          }
        }
        """

        variables = {
            "orderId": order_id
        }

        result = await self.execute_query(query, variables, "getDialogOrder")
        return result
