import argparse
from getpass import getpass

from sqlalchemy import select

from .db import Audit, DB, User
from .main import hasher
from .schemas import Credentials


def main():
    parser = argparse.ArgumentParser(description="Provision a ClipForge administrator")
    parser.add_argument("email")
    args = parser.parse_args()
    password = getpass("Password: ")
    if password != getpass("Confirm password: "):
        parser.error("Passwords did not match")
    data = Credentials(email=args.email, password=password)
    with DB() as db:
        if db.scalar(select(User).where(User.email == data.email.lower())):
            parser.error("An account already uses that email")
        user = User(email=data.email.lower(), password_hash=hasher.hash(data.password), admin=True)
        db.add(user)
        db.flush()
        db.add(Audit(owner_id=user.id, action="admin_provisioned"))
        db.commit()
    print("Administrator created")


if __name__ == "__main__":
    main()
