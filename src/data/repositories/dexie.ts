import type { Collection, Table, WhereClause } from 'dexie'
import type { ShallowRef } from 'vue'
import { observe, observeWithDeps } from '../observe'
import type { QuerySpec, Repository, WhereValue } from '../types'

function isInValue<V>(v: WhereValue<V>): v is { in: V[] } {
  return typeof v === 'object' && v !== null && 'in' in (v as object) && Array.isArray((v as { in: V[] }).in)
}

function buildCollection<T, K extends string>(table: Table<T, K>, spec: QuerySpec<T>): Collection<T, K> {
  const where = spec.where ?? {}
  const entries = Object.entries(where) as [string, WhereValue<unknown>][]

  if (entries.length === 0) return table.toCollection()

  // Single-field path uses Dexie's indexed where
  if (entries.length === 1) {
    const [field, value] = entries[0]
    const clause: WhereClause<T, K> = table.where(field)
    if (isInValue(value)) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return clause.anyOf(value.in as any[])
    }
    if (Array.isArray(value)) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return clause.anyOf(value as any[])
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return clause.equals(value as any)
  }

  // Multi-field path falls back to filter
  return table.filter(row => {
    for (const [field, value] of entries) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const v = (row as any)[field]
      if (isInValue(value)) {
        if (!value.in.includes(v)) return false
      } else if (Array.isArray(value)) {
        if (!value.includes(v)) return false
      } else if (v !== value) {
        return false
      }
    }
    return true
  })
}

export function createDexieRepository<T, K extends string = string>(getTable: () => Table<T, K>): Repository<T, K> {
  const t = () => getTable()

  return {
    table: t,

    get: id => t().get(id),
    list: () => t().toArray(),
    find: spec => buildCollection(t(), spec).toArray(),
    findKeys: spec => buildCollection(t(), spec).primaryKeys() as Promise<K[]>,
    findFirst: spec => buildCollection(t(), spec).first(),
    count: spec => (spec ? buildCollection(t(), spec).count() : t().count()),

    add: value => t().add(value) as Promise<K>,
    put: value => t().put(value) as Promise<K>,
    bulkPut: values => t().bulkPut(values) as Promise<K>,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    update: (id, changes) => t().update(id, changes as any) as Promise<number>,
    delete: id => t().delete(id),
    bulkDelete: ids => t().bulkDelete(ids),
    deleteWhere: spec => buildCollection(t(), spec).delete(),
    modifyWhere: (spec, changes) =>
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      buildCollection(t(), spec).modify(changes as any) as unknown as Promise<number>,
    modifyAll: (predicate, changes) =>
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      t().filter(predicate).modify(changes as any) as unknown as Promise<number>,

    observeList: <I = T[]>(options: { initialValue?: I } = {}) =>
      observe<T[], I>(() => t().toArray(), options) as ShallowRef<T[] | I>,

    observeFind: <I = T[]>(
      spec: QuerySpec<T> | (() => QuerySpec<T>),
      options: { initialValue?: I; deps?: unknown } = {}
    ) => {
      const { deps, ...rest } = options
      if (typeof spec === 'function') {
        if (deps !== undefined) {
          return observeWithDeps<T[], I>(deps, () => buildCollection(t(), spec()).toArray(), rest) as ShallowRef<T[] | I>
        }
        return observe<T[], I>(() => buildCollection(t(), spec()).toArray(), rest) as ShallowRef<T[] | I>
      }
      return observe<T[], I>(() => buildCollection(t(), spec).toArray(), rest) as ShallowRef<T[] | I>
    },

    observeOne: <I = T | undefined>(
      id: K | (() => K),
      options: { initialValue?: I; deps?: unknown } = {}
    ) => {
      const { deps, ...rest } = options
      if (typeof id === 'function') {
        if (deps !== undefined) {
          return observeWithDeps<T | undefined, I>(deps, () => t().get((id as () => K)()), rest) as ShallowRef<T | I | undefined>
        }
        return observe<T | undefined, I>(() => t().get((id as () => K)()), rest) as ShallowRef<T | I | undefined>
      }
      return observe<T | undefined, I>(() => t().get(id), rest) as ShallowRef<T | I | undefined>
    }
  }
}
