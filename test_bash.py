import pty
import os

pty.spawn(["bash", "-c", "read -p 'Enter: ' X; echo $X"])
