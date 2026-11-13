CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    stock INTEGER NOT NULL CHECK (stock >= 0),
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);
