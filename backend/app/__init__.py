# Runs before any other app module is imported. setup_tls() must happen here, before
# boto3/urllib3 are imported: truststore swaps ssl.SSLContext, and a library that
# grabbed the original class earlier ends up in infinite recursion. (Local dev only;
# USE_OS_TRUSTSTORE is false in production.)
from app.config import setup_tls

setup_tls()
