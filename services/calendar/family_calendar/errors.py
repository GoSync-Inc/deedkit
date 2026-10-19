class CalendarError(Exception):
    """Public fixed error code; never include provider payloads or credentials.

    `field_errors` names the input fields at fault, as [{"field": path, "reason": fixed word}].
    """

    def __init__(self, code: str, field_errors: list | None = None):
        self.code = code
        self.field_errors = field_errors or []
        super().__init__(code)
