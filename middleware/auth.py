# middleware/auth.py
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import jwt, JWTError
from passlib.context import CryptContext

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------
# ENV CONFIG
# ---------------------------------------------------------
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

SECRET_KEY = os.getenv("JWT_SECRET", "changeme")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))

# debug prints so you can see what’s going on
print("ADMIN_USERNAME from env:", ADMIN_USERNAME)
print("ADMIN_PASSWORD_HASH is set:", ADMIN_PASSWORD_HASH is not None)

# if these are missing, we *want* to know loudly
if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
    print("WARNING: ADMIN_USERNAME or ADMIN_PASSWORD_HASH not set in .env!")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ---------------------------------------------------------
# LOGIN ROUTE
# ---------------------------------------------------------
@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    # Hard fail if env not configured
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin credentials are not configured on server",
        )

    if form_data.username != ADMIN_USERNAME:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or password",
        )

    try:
        password_ok = verify_password(form_data.password, ADMIN_PASSWORD_HASH)
    except Exception as e:
        # This is where `hashed` being None would have crashed before
        print("Error verifying password:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Password verification error on server",
        )

    if not password_ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or password",
        )

    access_token = create_access_token({"sub": ADMIN_USERNAME})
    return {"access_token": access_token, "token_type": "bearer"}


# ---------------------------------------------------------
# ADMIN GUARD
# ---------------------------------------------------------
def require_admin(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username != ADMIN_USERNAME:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized"
            )
    except JWTError as e:
        print("JWT decode error:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token"
        )

    return True
