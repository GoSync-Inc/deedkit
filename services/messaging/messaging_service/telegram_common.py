"""Names the Telegram collector and its mixin modules share: the provider error classes, the transient
database faults and the evidence kept of a chat's provider error (moved from telegram_collector.py, W5-4a)."""
import re


class ProviderUnavailable(Exception): pass


class ProviderPageError(Exception):
    """A non-RPC failure inside one Telethon history read; keeps only the class name, never its text."""
    def __init__(self,error_class):
        self.error_class=error_class; super().__init__('provider_page_error')


def database_errors():
    # Transient archive faults: statement timeouts, lost connections and a
    # saturated pool. Lease loss is a RuntimeError and still stops the collector.
    import psycopg
    from psycopg_pool import PoolTimeout
    return (psycopg.OperationalError,PoolTimeout)


def chat_error_evidence(error):
    """Class name and Telegram's fixed RPC code of a chat's provider error; never exception text."""
    name=error.error_class if isinstance(error,ProviderPageError) else type(error).__name__
    evidence={'error_class':name if type(name) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',name) else 'unknown'}
    message=getattr(error,'message',None) if type(error).__module__.startswith('telethon.') else None
    if type(message) is str and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',message):evidence['rpc_message']=message
    return evidence
