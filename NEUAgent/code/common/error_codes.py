from __future__ import annotations


class ErrorCode:
    SUCCESS = 0
    UNKNOWN_ERROR = 1

    INVALID_INPUT = 1000
    MISSING_REQUIRED_PARAM = 1001
    INVALID_PARAM_TYPE = 1002
    INVALID_PARAM_VALUE = 1003
    PARAM_OUT_OF_RANGE = 1004

    FILE_NOT_FOUND = 2000
    FILE_READ_ERROR = 2001
    FILE_WRITE_ERROR = 2002
    FILE_PERMISSION_DENIED = 2003
    FILE_TOO_LARGE = 2004
    UNSUPPORTED_FILE_TYPE = 2005

    EXECUTION_TIMEOUT = 3000
    EXECUTION_ERROR = 3001
    EXECUTION_FORBIDDEN = 3002
    RESOURCE_LIMIT_EXCEEDED = 3003

    TOOL_NOT_FOUND = 4000
    TOOL_SCHEMA_INVALID = 4001
    TOOL_CALL_FAILED = 4002
    TOOL_VALIDATION_FAILED = 4003

    SKILL_NOT_FOUND = 5000
    SKILL_LOAD_ERROR = 5001
    SKILL_EXECUTION_ERROR = 5002


ERROR_MESSAGES = {
    ErrorCode.SUCCESS: "Success",
    ErrorCode.UNKNOWN_ERROR: "Unknown error occurred",
    ErrorCode.INVALID_INPUT: "Invalid input data",
    ErrorCode.MISSING_REQUIRED_PARAM: "Missing required parameter",
    ErrorCode.INVALID_PARAM_TYPE: "Invalid parameter type",
    ErrorCode.INVALID_PARAM_VALUE: "Invalid parameter value",
    ErrorCode.PARAM_OUT_OF_RANGE: "Parameter value out of allowed range",
    ErrorCode.FILE_NOT_FOUND: "File not found",
    ErrorCode.FILE_READ_ERROR: "Failed to read file",
    ErrorCode.FILE_WRITE_ERROR: "Failed to write file",
    ErrorCode.FILE_PERMISSION_DENIED: "File permission denied",
    ErrorCode.FILE_TOO_LARGE: "File size exceeds limit",
    ErrorCode.UNSUPPORTED_FILE_TYPE: "Unsupported file type",
    ErrorCode.EXECUTION_TIMEOUT: "Execution timed out",
    ErrorCode.EXECUTION_ERROR: "Execution error",
    ErrorCode.EXECUTION_FORBIDDEN: "Operation forbidden",
    ErrorCode.RESOURCE_LIMIT_EXCEEDED: "Resource limit exceeded",
    ErrorCode.TOOL_NOT_FOUND: "Tool not found",
    ErrorCode.TOOL_SCHEMA_INVALID: "Invalid tool schema",
    ErrorCode.TOOL_CALL_FAILED: "Tool call execution failed",
    ErrorCode.TOOL_VALIDATION_FAILED: "Tool call validation failed",
    ErrorCode.SKILL_NOT_FOUND: "Skill not found",
    ErrorCode.SKILL_LOAD_ERROR: "Failed to load skill",
    ErrorCode.SKILL_EXECUTION_ERROR: "Skill execution error",
}


def get_error_message(code: int) -> str:
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[ErrorCode.UNKNOWN_ERROR])


def classify_error(exception: Exception) -> tuple[int, str]:
    error_type = type(exception).__name__
    error_msg = str(exception)

    if isinstance(exception, FileNotFoundError):
        return ErrorCode.FILE_NOT_FOUND, error_msg
    if isinstance(exception, PermissionError):
        return ErrorCode.FILE_PERMISSION_DENIED, error_msg
    if isinstance(exception, TimeoutError):
        return ErrorCode.EXECUTION_TIMEOUT, error_msg
    if isinstance(exception, ValueError):
        if "not found" in error_msg.lower():
            return ErrorCode.FILE_NOT_FOUND, error_msg
        if "must be" in error_msg.lower() or "invalid" in error_msg.lower():
            return ErrorCode.INVALID_PARAM_VALUE, error_msg
        if "too long" in error_msg.lower() or "exceed" in error_msg.lower():
            return ErrorCode.PARAM_OUT_OF_RANGE, error_msg
        if "forbidden" in error_msg.lower():
            return ErrorCode.EXECUTION_FORBIDDEN, error_msg
        return ErrorCode.INVALID_PARAM_VALUE, error_msg
    if isinstance(exception, TypeError):
        return ErrorCode.INVALID_PARAM_TYPE, error_msg
    if isinstance(exception, KeyError):
        return ErrorCode.MISSING_REQUIRED_PARAM, error_msg

    if "timeout" in error_msg.lower():
        return ErrorCode.EXECUTION_TIMEOUT, error_msg
    if "permission" in error_msg.lower():
        return ErrorCode.FILE_PERMISSION_DENIED, error_msg
    if "not found" in error_msg.lower():
        return ErrorCode.FILE_NOT_FOUND, error_msg

    return ErrorCode.UNKNOWN_ERROR, error_msg


def make_error_result(error_code: int, error_msg: str, skill_name: str | None = None) -> dict:
    return {
        "error_code": error_code,
        "error_message": error_msg,
        "error_category": get_error_category(error_code),
        "skill_name": skill_name,
        "recoverable": is_recoverable(error_code),
    }


def get_error_category(code: int) -> str:
    if code == ErrorCode.SUCCESS:
        return "success"
    if 1000 <= code < 2000:
        return "input_validation"
    if 2000 <= code < 3000:
        return "file_system"
    if 3000 <= code < 4000:
        return "execution"
    if 4000 <= code < 5000:
        return "tool_system"
    if 5000 <= code < 6000:
        return "skill_system"
    return "unknown"


def is_recoverable(code: int) -> bool:
    return code in {
        ErrorCode.EXECUTION_TIMEOUT,
        ErrorCode.FILE_NOT_FOUND,
        ErrorCode.TOOL_NOT_FOUND,
        ErrorCode.SKILL_NOT_FOUND,
    }
