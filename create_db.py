import sqlite3

conn = sqlite3.connect("sample.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT,
    email TEXT,
    country TEXT
)
""")

cursor.execute("""
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    product TEXT,
    amount REAL,
    order_date TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
)
""")

customers = [
    (1, "Alice", "alice@mail.com", "USA"),
    (2, "Bob", "bob@mail.com", "Canada"),
    (3, "Charlie", "charlie@mail.com", "UK"),
]

orders = [
    (1, 1, "Laptop", 1200, "2024-01-10"),
    (2, 1, "Mouse", 25, "2024-01-12"),
    (3, 2, "Keyboard", 80, "2024-02-01"),
    (4, 3, "Monitor", 300, "2024-02-10"),
]

cursor.executemany("INSERT INTO customers VALUES (?,?,?,?)", customers)
cursor.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)

conn.commit()
conn.close()

print("Sample SQLite database created: sample.db")
