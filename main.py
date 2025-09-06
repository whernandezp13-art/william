# ==========================
# Importaciones
# ==========================
from fastapi import FastAPI, HTTPException              # Framework para crear la API y manejar errores HTTP
from fastapi.middleware.cors import CORSMiddleware      # Middleware para permitir CORS (consumir API desde otras apps)
from pydantic import BaseModel, Field, PositiveFloat, conint, constr  # Validaciones y tipos para los modelos
from typing import Optional, List                       # Anotaciones de tipos opcionales y listas
from datetime import datetime                           # Manejo de fechas (creado_en)
import os, json, asyncio                                # Archivos, JSON y concurrencia asíncrona

# ==========================
# Instancia principal de la aplicación
# ==========================
app = FastAPI(title="API Registro de Productos", version="1.0.0")

# ==========================
# Configuración de CORS
# ==========================
# Permite que el frontend (incluso si está en otra URL/puerto) consuma esta API.
# En producción es recomendable RESTRINGIR allow_origins a dominios conocidos.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # ⚠️ Abrir a todos los orígenes. En producción: especificar dominios.
    allow_credentials=True,
    allow_methods=["*"],   # Permitir todos los métodos HTTP (GET, POST, etc.)
    allow_headers=["*"],   # Permitir todos los encabezados
)

# ==========================
# Modelos de datos (Pydantic)
# ==========================
class ProductoIn(BaseModel):
    """
    Datos de ENTRADA para crear un producto.
    Se validan longitudes, tipos y valores mínimos.
    """
    nombre: constr(strip_whitespace=True, min_length=2, max_length=60)
    categoria: Optional[constr(strip_whitespace=True, max_length=40)] = None
    precio: PositiveFloat = Field(..., description="Precio > 0")  # Debe ser > 0
    stock: conint(ge=0) = Field(0, description="Stock >= 0")      # Entero >= 0, por defecto 0
    descripcion: Optional[constr(max_length=200)] = None

class ProductoOut(ProductoIn):
    """
    Datos de SALIDA al cliente (incluye ID y fecha de creación).
    """
    id: int
    creado_en: datetime  # Se almacenará como datetime; se serializa a ISO en JSON

# ==========================
# Configuración de archivos / almacenamiento
# ==========================
DATA_DIR = "data"                                             # Carpeta donde se guardan los archivos
TEXT_FILE = os.path.join(DATA_DIR, "productos.txt")           # Archivo de texto legible (pipe-separated)
JSONL_FILE = os.path.join(DATA_DIR, "productos.jsonl")        # Archivo JSONL (un JSON por línea)
os.makedirs(DATA_DIR, exist_ok=True)                          # Crear carpeta data/ si no existe

# Si el archivo de texto no existe, escribir cabecera
if not os.path.exists(TEXT_FILE):
    with open(TEXT_FILE, "w", encoding="utf-8") as f:
        f.write("id|nombre|categoria|precio|stock|creado_en|descripcion\n")

def _cargar_ultimo_id() -> int:
    """
    Lee productos.jsonl (si existe) para recuperar el último ID utilizado.
    Permite que la API continúe incrementando IDs aun tras reinicios.
    """
    if not os.path.exists(JSONL_FILE):
        return 0
    last_id = 0
    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                last_id = max(last_id, int(obj.get("id", 0)))
            except json.JSONDecodeError:
                # Si hay líneas corruptas, se ignoran para no romper el flujo
                continue
    return last_id

# Estado en memoria
ultimo_id = _cargar_ultimo_id()      # Último ID usado (persistido por jsonl)
lock = asyncio.Lock()                # Lock para evitar condiciones de carrera en concurrencia

# ==========================
# Utilidades internas
# ==========================
def _to_text_line(p: ProductoOut) -> str:
    """
    Convierte un producto a una línea para productos.txt con separador '|'.
    Se escapan saltos de línea y pipes para no romper el formato.
    """
    def esc(s: Optional[str]) -> str:
        if s is None:
            return ""
        return str(s).replace("\n", " ").replace("|", "/")
    # creado_en se serializa en ISO 8601
    return f"{p.id}|{esc(p.nombre)}|{esc(p.categoria)}|{p.precio}|{p.stock}|{p.creado_en.isoformat()}|{esc(p.descripcion)}\n"

def _guardar_producto(p: ProductoOut) -> None:
    """
    Persiste el producto en:
    - productos.txt (humano-legible, separado por '|')
    - productos.jsonl (máquina-legible, un JSON por línea)
    Maneja diferencias entre Pydantic v1 y v2.
    """
    # 1) Guardar en TXT
    with open(TEXT_FILE, "a", encoding="utf-8") as f:
        f.write(_to_text_line(p))

    # 2) Preparar dict serializable para JSON
    try:
        # Pydantic v2: convierte datetime a string ISO automáticamente
        data = p.model_dump(mode="json")
    except Exception:
        # Pydantic v1
        try:
            data = p.dict()
        except Exception:
            # Último recurso (no debería ocurrir)
            data = p.__dict__

    # 3) Guardar en JSONL (asegurando serialización de cualquier tipo no JSON con default=str)
    with open(JSONL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")

def _leer_todos() -> List[ProductoOut]:
    """
    Lee todos los productos desde productos.jsonl y los convierte a objetos ProductoOut.
    Si hay líneas corruptas, se ignoran (no detienen el proceso).
    """
    res: List[ProductoOut] = []
    if not os.path.exists(JSONL_FILE):
        return res

    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                # Compatibilidad: si creado_en viene como string ISO, parsearlo a datetime
                if isinstance(d.get("creado_en"), str):
                    d["creado_en"] = datetime.fromisoformat(d["creado_en"])
                res.append(ProductoOut(**d))
            except Exception:
                # Ignorar y continuar si una línea específica está mal
                continue
    return res

def _leer_por_id(producto_id: int) -> Optional[ProductoOut]:
    """
    Busca un producto específico por ID dentro de productos.jsonl.
    Devuelve ProductoOut o None si no existe.
    """
    if not os.path.exists(JSONL_FILE):
        return None

    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if int(d.get("id", -1)) == producto_id:
                    if isinstance(d.get("creado_en"), str):
                        d["creado_en"] = datetime.fromisoformat(d["creado_en"])
                    return ProductoOut(**d)
            except Exception:
                continue
    return None

# ==========================
# Endpoints públicos de la API
# ==========================
@app.get("/", tags=["info"])
def raiz():
    """
    Endpoint informativo.
    Útil para verificar rutas de archivos y endpoints disponibles.
    """
    return {
        "mensaje": "API de Registro de Productos",
        "endpoints": {
            "crear": "POST /productos",
            "listar": "GET /productos",
            "detalle": "GET /productos/{id}"
        },
        "archivos": {
            "texto": TEXT_FILE,
            "jsonl": JSONL_FILE
        }
    }

@app.post("/productos", response_model=ProductoOut, tags=["productos"], status_code=201)
async def crear_producto(data: ProductoIn):
    """
    Crea un nuevo producto.
    - Incrementa el ID de forma segura con un lock (evita carreras).
    - Asigna la fecha/hora de creación.
    - Persiste en TXT y JSONL.
    """
    global ultimo_id
    async with lock:  # Garantiza exclusión mutua durante incremento y escritura
        ultimo_id += 1
        producto = ProductoOut(
            id=ultimo_id,
            creado_en=datetime.utcnow(),  # Fecha/hora actual (naive). Si prefieres tz: usar datetime.now(timezone.utc)
            **data.model_dump()
        )
        _guardar_producto(producto)
        return producto

@app.get("/productos", response_model=List[ProductoOut], tags=["productos"])
def listar_productos():
    """
    Devuelve la lista completa de productos almacenados.
    """
    return _leer_todos()

@app.get("/productos/{producto_id}", response_model=ProductoOut, tags=["productos"])
def obtener_producto(producto_id: int):
    """
    Devuelve un producto específico por ID.
    Si no existe, responde 404.
    """
    p = _leer_por_id(producto_id)
    if not p:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return p
