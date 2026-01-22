import os
import subprocess

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

# We load the env vars from a .env file
load_dotenv()

# Token uses by master to send commands to this node
TOKEN = os.getenv("API_TOKEN")


class Command(BaseModel):
    token: str = Field(..., description="Authentication token")
    command: str = Field(..., description="Shell command to execute")


app = FastAPI(title="Wireguard API", version="0.2.0")


@app.post("/", status_code=status.HTTP_200_OK)
async def run_command(command: Command) -> dict[str, str]:
    if command.token != TOKEN:
        # Using 403 Forbidden for invalid token
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid authentication token"
        )

    try:
        # Note: shell=True is dangerous but required for this specific use case
        # as per legacy functionality.
        output = subprocess.check_output(
            command.command,
            shell=True,
            stderr=subprocess.STDOUT,
            text=True,  # Ensure output is string, not bytes
        )
        return {"status": output.strip()}
    except subprocess.CalledProcessError as e:
        return {"status": f"Error: {e.output}"}
    except Exception as e:
        return {"status": str(e)}
