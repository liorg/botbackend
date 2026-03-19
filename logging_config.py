"""
logging_config.py - Centralized logging for VID FastAPI
Google Cloud Logging compatible + local development support
"""

import logging
import sys
import os
import json
from datetime import datetime
from functools import lru_cache

IS_CLOUD = bool(os.getenv("K_SERVICE") or os.getenv("GOOGLE_CLOUD_PROJECT"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "logger": record.name,
            "function": record.funcName,
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        standard_attrs = {
            "name", "msg", "args", "created", "filename", "funcName",
            "levelname", "levelno", "lineno", "module", "msecs",
            "pathname", "process", "processName", "relativeCreated",
            "stack_info", "exc_info", "exc_text", "thread", "threadName",
            "message", "asctime"
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                try:
                    json.dumps(value)
                    log_entry[key] = value
                except:
                    log_entry[key] = str(value)
        
        return json.dumps(log_entry, ensure_ascii=False)


class LocalFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"
    DIM = "\033[2m"
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        level = f"{color}{record.levelname:8}{self.RESET}"
        location = f"{self.DIM}{record.name}.{record.funcName}{self.RESET}"
        message = record.getMessage()
        
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
        output = f"{self.DIM}{timestamp}{self.RESET} | {level} | {location} | {message}{extra_str}"
        
        if record.exc_info:
            output += f"\n{self.COLORS['ERROR']}{self.formatException(record.exc_info)}{self.RESET}"
        
        return output


@lru_cache(maxsize=32)
def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f"vid.{name}")
    
    if logger.handlers:
        return logger
    
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    
    if IS_CLOUD:
        handler.setFormatter(CloudLoggingFormatter())
    else:
        handler.setFormatter(LocalFormatter())
    
    logger.addHandler(handler)
    logger.propagate = False
    
    return logger


async def logging_middleware(request, call_next):
    import time
    
    logger = get_logger("http")
    start_time = time.time()
    
    response = await call_next(request)
    
    duration_ms = (time.time() - start_time) * 1000
    
    logger.info(f"{request.method} {request.url.path} → {response.status_code}", extra={
        "method": request.method,
        "path": request.url.path,
        "status_code": response.status_code,
        "duration_ms": round(duration_ms, 2),
    })
    
    return response