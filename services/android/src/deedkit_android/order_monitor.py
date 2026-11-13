"""One bounded delivery step, driven by the existing host lifecycle and device gate."""
class OrderMonitor:
    def __init__(self, events, *, submit=None, grok_submit=None):
        self.events, self.submit, self.grok_submit = events, submit, grok_submit

    async def dispatch_once(self, wake_id):
        wake = self.events.wake(wake_id)
        if wake['state'] != 'pending':
            return
        submit = self.submit if wake['target'] == 'codex' else self.grok_submit
        if submit is None:
            self.events.hold(wake_id, 'wake_receiver_unavailable')
            return
        if not self.events.claim(wake_id):
            return
        envelope = self.events.envelope(wake_id)
        try:
            receipt = await submit(envelope['owner'], envelope['device_id'], envelope['original_request_id'],
                                   native_thread_id=envelope['native_thread_id'],
                                   native_turn_id=envelope['native_turn_id'], kind=envelope['kind'],
                                   payload=envelope['payload'] | {'question_id': envelope['question_id']})
            self.events.complete(wake_id, receipt)
        except BaseException as error:
            # Cancellation, malformed receipt and lost reply may all have crossed the write boundary.
            self.events.unknown(wake_id)
            if not isinstance(error, Exception):
                raise
