"""Network monitoring for API requests and responses."""

import json
from typing import Dict, Any, Optional, Callable
from urllib.parse import urlparse

from playwright.async_api import Page, Request, Response

from . import logger as logging


class NetworkMonitor:
    """Monitors and logs network requests/responses for API debugging."""

    def __init__(self, page: Page, enabled: bool = True):
        self.page = page
        self.enabled = enabled
        self.request_log: list = []
        self.response_log: list = []
        self.graphql_requests: list = []
        self.api_errors: list = []

        if enabled:
            self._setup_listeners()

    def _setup_listeners(self):
        """Setup network event listeners."""
        self.page.on("request", self._log_request)
        self.page.on("response", self._log_response)

    def _log_request(self, request: Request):
        """Log outgoing request."""
        request_info = {
            "method": request.method,
            "url": request.url,
            "headers": dict(request.headers),
            "timestamp": self._get_timestamp(),
            "resource_type": request.resource_type
        }

        # Log POST data for API calls
        if request.post_data:
            try:
                # Try to parse as JSON
                data = json.loads(request.post_data)
                request_info["post_data"] = data

                # Special handling for GraphQL
                if self._is_graphql_request(request.url, data):
                    self._log_graphql_request(request_info, data)

            except json.JSONDecodeError:
                # Raw data
                request_info["post_data_raw"] = request.post_data[:500]  # Limit size

        self.request_log.append(request_info)

        # Log important requests
        if self._should_log_request(request):
            self._print_request(request_info)

    def _log_response(self, response: Response):
        """Log incoming response."""
        response_info = {
            "status": response.status,
            "url": response.url,
            "headers": dict(response.headers),
            "timestamp": self._get_timestamp()
        }

        self.response_log.append(response_info)

        # Check for API errors
        if self._is_api_error(response):
            self.api_errors.append({
                "url": response.url,
                "status": response.status,
                "timestamp": self._get_timestamp()
            })

        # Log important responses
        if self._should_log_response(response):
            self._print_response(response_info)

    def _is_graphql_request(self, url: str, data: Dict[str, Any]) -> bool:
        """Check if request is GraphQL."""
        return (
            "graphql" in url.lower() and
            isinstance(data, dict) and
            ("query" in data or "mutation" in data)
        )

    def _log_graphql_request(self, request_info: Dict, data: Dict[str, Any]):
        """Log GraphQL request details."""
        operation = data.get("operationName", "unknown")
        query_type = "query"
        if "mutation" in data:
            query_type = "mutation"

        # Extract main operation from query
        query = data.get("query", "")
        if "addComment" in query:
            operation = "addComment"
        elif "getDialog" in query:
            operation = "getDialog"
        elif "getOrder" in query:
            operation = "getOrder"

        graphql_info = {
            **request_info,
            "operation": operation,
            "type": query_type,
            "variables": data.get("variables", {})
        }

        self.graphql_requests.append(graphql_info)

        # Log GraphQL operations
        vars_preview = ""
        if graphql_info["variables"]:
            vars_str = json.dumps(graphql_info["variables"], ensure_ascii=False)
            vars_preview = f" vars: {vars_str[:100]}..." if len(vars_str) > 100 else f" vars: {vars_str}"

        logging.info(f"🔍 GraphQL {query_type}: {operation}{vars_preview}")

    def _should_log_request(self, request: Request) -> bool:
        """Check if request should be logged."""
        url = request.url.lower()
        return (
            "graphql" in url or
            "api" in url or
            request.method in ["POST", "PUT", "PATCH"]
        )

    def _should_log_response(self, response: Response) -> bool:
        """Check if response should be logged."""
        url = response.url.lower()
        return (
            "graphql" in url or
            "api" in url or
            response.status >= 400  # Errors
        )

    def _is_api_error(self, response: Response) -> bool:
        """Check if response indicates API error."""
        return (
            response.status >= 400 and
            ("graphql" in response.url.lower() or "api" in response.url.lower())
        )

    def _print_request(self, request_info: Dict):
        """Print formatted request info."""
        method = request_info["method"]
        url = urlparse(request_info["url"]).path  # Only path part

        if "post_data" in request_info:
            data_preview = json.dumps(request_info["post_data"], ensure_ascii=False)[:100]
            logging.info(f"📤 {method} {url} → {data_preview}...")
        else:
            logging.info(f"📤 {method} {url}")

    def _print_response(self, response_info: Dict):
        """Print formatted response info."""
        status = response_info["status"]
        url = urlparse(response_info["url"]).path

        if status >= 400:
            logging.error(f"📥 {status} {url} - Ошибка API")
        else:
            logging.info(f"📥 {status} {url}")

    def _get_timestamp(self) -> str:
        """Get current timestamp."""
        from datetime import datetime
        return datetime.now().isoformat()

    def get_stats(self) -> Dict[str, Any]:
        """Get network monitoring statistics."""
        return {
            "total_requests": len(self.request_log),
            "total_responses": len(self.response_log),
            "graphql_requests": len(self.graphql_requests),
            "api_errors": len(self.api_errors),
            "enabled": self.enabled
        }

    def get_recent_graphql(self, limit: int = 5) -> list:
        """Get recent GraphQL requests."""
        return self.graphql_requests[-limit:] if self.graphql_requests else []

    def get_api_errors(self, limit: int = 10) -> list:
        """Get recent API errors."""
        return self.api_errors[-limit:] if self.api_errors else []

    def clear_logs(self):
        """Clear all logs."""
        self.request_log.clear()
        self.response_log.clear()
        self.graphql_requests.clear()
        self.api_errors.clear()
        logging.info("🧹 Логи сетевого мониторинга очищены")
