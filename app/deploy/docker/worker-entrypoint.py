"""An absolute entrypoint keeps imports fixed when the container cwd is /runtime or /workspace."""
import asyncio
import os
from carme.worker import main

os.environ["TMPDIR"] = "/runtime"
asyncio.run(main())
