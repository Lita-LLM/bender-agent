"""Ed25519 по RFC 8032, без зависимостей; строгая проверка каноничности и подгруппы.

Проверка не хранит секретов. Функции подписи нужны для локальной упаковки/векторов;
реализация на больших целых Python не обещает постоянного времени исполнения.
"""
from hashlib import sha512

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P-2, P) % P
I = pow(2, (P-1)//4, P)
IDENTITY = (0, 1, 1, 0)


def add(p, q):
    x,y,z,t = p
    u,v,w,s = q
    a,b = (y-x)*(v-u)%P, (y+x)*(v+u)%P
    c,d = 2*D*t*s%P, 2*z*w%P
    e,f,g,h = b-a,d-c,d+c,b+a
    return e*f%P,g*h%P,f*g%P,e*h%P


def mul(p, n):
    result = IDENTITY
    while n:
        if n & 1:
            result = add(result,p)
        p = add(p,p)
        n >>= 1
    return result


def encode(p):
    x,y,z,_ = p
    inv = pow(z,P-2,P)
    x,y = x*inv%P,y*inv%P
    return (y | ((x&1)<<255)).to_bytes(32,"little")


def decode(raw):
    if len(raw)!=32:
        raise ValueError("Длина точки Ed25519")
    n=int.from_bytes(raw,"little")
    y,sign=n&((1<<255)-1),n>>255
    if y>=P:
        raise ValueError("Неканоничная точка Ed25519")
    square=(y*y-1)*pow(D*y*y+1,P-2,P)%P
    x=pow(square,(P+3)//8,P)
    if x*x%P!=square:
        x=x*I%P
    if x*x%P!=square or (x==0 and sign):
        raise ValueError("Некорректная точка Ed25519")
    if x&1!=sign:
        x=P-x
    return x,y,1,x*y%P


B = decode(bytes.fromhex("5866666666666666666666666666666666666666666666666666666666666666"))


def secret(seed):
    if len(seed)!=32:
        raise ValueError("Начальное значение Ed25519 должно содержать 32 байта")
    hashed=sha512(seed).digest()
    scalar=int.from_bytes(hashed[:32],"little")
    return (scalar & ((1<<254)-8)) | (1<<254), hashed[32:]


def public_key(seed):
    scalar,_=secret(seed)
    return encode(mul(B,scalar))


def sign(seed, message):
    scalar,prefix=secret(seed)
    public=encode(mul(B,scalar))
    r=int.from_bytes(sha512(prefix+message).digest(),"little")%L
    point=encode(mul(B,r))
    h=int.from_bytes(sha512(point+public+message).digest(),"little")%L
    return point+((r+h*scalar)%L).to_bytes(32,"little")


def verify(public, message, signature):
    try:
        if len(signature)!=64:
            return False
        a,r=decode(public),decode(signature[:32])
        s=int.from_bytes(signature[32:],"little")
        # Запрещаем малый порядок и смешанную подгруппу, а также S+L.
        identity=encode(IDENTITY)
        if s>=L or encode(a)==identity or encode(r)==identity or encode(mul(a,L))!=identity or encode(mul(r,L))!=identity:
            return False
        h=int.from_bytes(sha512(signature[:32]+public+message).digest(),"little")%L
        return encode(mul(B,s))==encode(add(r,mul(a,h)))
    except (ValueError, TypeError, OverflowError):
        return False
