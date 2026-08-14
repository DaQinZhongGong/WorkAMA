const DB_NAME = 'workama-mobile'
const DB_VERSION = 1
const MESSAGE_STORE = 'messages'

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onerror = () => reject(request.error)
    request.onsuccess = () => resolve(request.result)
    request.onupgradeneeded = (event) => {
      const db = (event.target as IDBOpenDBRequest).result
      if (!db.objectStoreNames.contains(MESSAGE_STORE)) {
        const store = db.createObjectStore(MESSAGE_STORE, { keyPath: 'id' })
        store.createIndex('sessionId', 'sessionId', { unique: false })
        store.createIndex('timestamp', 'timestamp', { unique: false })
      }
    }
  })
}

type CachedMessage = {
  id: string
  sessionId: string
  role: 'user' | 'assistant'
  content: string
  timestamp: number
}

export async function cacheMessage(sessionId: string, message: { id: string; role: 'user' | 'assistant'; content: string }) {
  try {
    const db = await openDB()
    const tx = db.transaction(MESSAGE_STORE, 'readwrite')
    const store = tx.objectStore(MESSAGE_STORE)
    const data: CachedMessage = {
      id: message.id,
      sessionId,
      role: message.role,
      content: message.content,
      timestamp: Date.now(),
    }
    await new Promise<void>((resolve, reject) => {
      const req = store.put(data)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
    await trimMessages(sessionId)
    db.close()
  } catch {
    // IndexedDB is best-effort; ignore errors in private mode or unsupported environments
  }
}

export async function getCachedMessages(sessionId: string): Promise<CachedMessage[]> {
  try {
    const db = await openDB()
    const tx = db.transaction(MESSAGE_STORE, 'readonly')
    const store = tx.objectStore(MESSAGE_STORE)
    const index = store.index('sessionId')
    const results = await new Promise<CachedMessage[]>((resolve, reject) => {
      const req = index.getAll(sessionId)
      req.onsuccess = () => resolve(req.result as CachedMessage[])
      req.onerror = () => reject(req.error)
    })
    db.close()
    return results.sort((a, b) => a.timestamp - b.timestamp)
  } catch {
    return []
  }
}

async function trimMessages(sessionId: string) {
  try {
    const db = await openDB()
    const tx = db.transaction(MESSAGE_STORE, 'readwrite')
    const store = tx.objectStore(MESSAGE_STORE)
    const index = store.index('sessionId')
    const all = await new Promise<CachedMessage[]>((resolve, reject) => {
      const req = index.getAll(sessionId)
      req.onsuccess = () => resolve(req.result as CachedMessage[])
      req.onerror = () => reject(req.error)
    })
    if (all.length > 10) {
      const toDelete = all.sort((a, b) => a.timestamp - b.timestamp).slice(0, all.length - 10)
      for (const item of toDelete) {
        store.delete(item.id)
      }
    }
    db.close()
  } catch {
    // ignore
  }
}

export async function clearCachedMessages(sessionId: string) {
  try {
    const db = await openDB()
    const tx = db.transaction(MESSAGE_STORE, 'readwrite')
    const store = tx.objectStore(MESSAGE_STORE)
    const index = store.index('sessionId')
    const all = await new Promise<CachedMessage[]>((resolve, reject) => {
      const req = index.getAll(sessionId)
      req.onsuccess = () => resolve(req.result as CachedMessage[])
      req.onerror = () => reject(req.error)
    })
    for (const item of all) {
      store.delete(item.id)
    }
    db.close()
  } catch {
    // ignore
  }
}
