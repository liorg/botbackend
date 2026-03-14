"""
logging_config.py - Centralized logging for VID FastAPI
Google Cloud Logging compatible + local development support

Usage in any router:
    from logging_config import get_logger
    logger = get_logger("auth")  # or "phones", "scenarios", etc.
    
    logger.info("User logged in", extra={"user_id": "123", "action": "login"})
"""

import logging
import sys
import os
import json
from datetime import datetime
from typing import Optional, Any
from functools import lru_cache

# ══════════════════════════════════════════════════════════════════════════════
# Environment Detection
# ══════════════════════════════════════════════════════════════════════════════

IS_CLOUD = bool(os.getenv("K_SERVICE") or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GAE_ENV"))
IS_DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# ══════════════════════════════════════════════════════════════════════════════
# Google Cloud Logging Formatter
# ══════════════════════════════════════════════════════════════════════════════

class CloudLoggingFormatter(logging.Formatter):
    """
    Format logs as JSON for Google Cloud Logging.
    
    Output format:
    {
        "severity": "INFO",
        "message": "User logged in",
        "timestamp": "2024-01-15T10:30:00.000Z",
        "module": "auth",
        "function": "login",
        "user_id": "abc123",
        "action": "login_success"
    }
    """
    
    # Map Python log levels to Google Cloud severity
    SEVERITY_MAP = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }
    
    def format(self, record: logging.LogRecord) -> str:
        # Base log entry
        log_entry = {
            "severity": self.SEVERITY_MAP.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields (user_id, action, etc.)
        # These come from logger.info("msg", extra={"user_id": "123"})
        standard_attrs = {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs",
            "pathname", "process", "processName", "relativeCreated",
            "stack_info", "exc_info", "exc_text", "thread", "threadName",
            "message", "asctime"
        }
        
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                # Serialize non-JSON-serializable objects
                try:
                    json.dumps(value)
                    log_entry[key] = value
                except (TypeError, ValueError):
                    log_entry[key] = str(value)
        
        return json.dumps(log_entry, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════════════
# Local Development Formatter (colored, readable)
# ══════════════════════════════════════════════════════════════════════════════

class LocalFormatter(logging.Formatter):
    """
    Colored, readable logs for local development.
    
    Output format:
    2024-01-15 10:30:00 | INFO     | auth.login | User logged in | user_id=abc123
    """
    
    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    def format(self, record: logging.LogRecord) -> str:
        # Get color for level
        color = self.COLORS.get(record.levelname, "")
        
        # Format timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Format level (padded)
        level = f"{color}{record.levelname:8}{self.RESET}"
        
        # Format location
        location = f"{self.DIM}{record.name}.{record.funcName}{self.RESET}"
        
        # Format message
        message = record.getMessage()
        
        # Format extra fields
        extras = []
        standard_attrs = {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs",
            "pathname", "process", "processName", "relativeCreated",
            "stack_info", "exc_info", "exc_text", "thread", "threadName",
            "message", "asctime"
        }
        
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                extras.append(f"{self.DIM}{key}={value}{self.RESET}")
        
        extra_str = " | " + " ".join(extras) if extras else ""
        
        # Build final message
        output = f"{self.DIM}{timestamp}{self.RESET} | {level} | {location} | {message}{extra_str}"
        
        # Add exception if present
        if record.exc_info:
            output += f"\n{self.COLORS['ERROR']}{self.formatException(record.exc_info)}{self.RESET}"
        
        return output


# ══════════════════════════════════════════════════════════════════════════════
# Logger Factory
# ══════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=32)
def get_logger(name: str) -> logging.Logger:
    """
    Get a configured logger for the given module name.
    
    Args:
        name: Module name (e.g., "auth", "phones", "scenarios")
    
    Returns:
        Configured logger instance
    
    Example:
        from logging_config import get_logger
        logger = get_logger("auth")
        
        logger.info("Login successful", extra={"user_id": user.id, "action": "login"})
        logger.warning("Invalid token", extra={"action": "auth_failed"})
        logger.error("Database error", exc_info=True)
    """
    logger = logging.getLogger(f"vid.{name}")
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger
    
    # Set level
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)
    
    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    
    # Choose formatter based on environment
    if IS_CLOUD:
        handler.setFormatter(CloudLoggingFormatter())
    else:
        handler.setFormatter(LocalFormatter())
    
    logger.addHandler(handler)
    
    # Don't propagate to root logger
    logger.propagate = False
    
    return logger


# ══════════════════════════════════════════════════════════════════════════════
# Request Logging Middleware
# ══════════════════════════════════════════════════════════════════════════════

def log_request(
    logger: logging.Logger,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    user_id: Optional[str] = None,
    extra: Optional[dict] = None
):
    """
    Log an HTTP request with standard fields.
    
    Args:
        logger: Logger instance
        method: HTTP method (GET, POST, etc.)
        path: Request path
        status_code: Response status code
        duration_ms: Request duration in milliseconds
        user_id: Optional user ID
        extra: Optional extra fields
    """
    log_data = {
        "action": "http_request",
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 2),
    }
    
    if user_id:
        log_data["user_id"] = user_id
    
    if extra:
        log_data.update(extra)
    
    # Choose log level based on status code
    if status_code >= 500:
        logger.error(f"{method} {path} → {status_code}", extra=log_data)
    elif status_code >= 400:
        logger.warning(f"{method} {path} → {status_code}", extra=log_data)
    else:
        logger.info(f"{method} {path} → {status_code}", extra=log_data)


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI Middleware
# ══════════════════════════════════════════════════════════════════════════════

async def logging_middleware(request, call_next):
    """
    FastAPI middleware for automatic request logging.
    
    Usage in main.py:
        from logging_config import logging_middleware
        app.middleware("http")(logging_middleware)
    """
    import time
    
    logger = get_logger("http")
    start_time = time.time()
    
    # Get user ID from JWT if present
    user_id = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            import jwt
            token = auth_header.replace("Bearer ", "")
            payload = jwt.decode(token, options={"verify_signature": False})
            user_id = payload.get("uid")
        except:
            pass
    
    # Process request
    response = await call_next(request)
    
    # Calculate duration
    duration_ms = (time.time() - start_time) * 1000
    
    # Log request
    log_request(
        logger=logger,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
        user_id=user_id,
    )
    
    return response


# ══════════════════════════════════════════════════════════════════════════════
# Convenience exports
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "get_logger",
    "log_request", 
    "logging_middleware",
    "IS_CLOUD",
    "IS_DEBUG",
]