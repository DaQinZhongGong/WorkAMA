import type { CommandSnapshot } from './types'

export interface Command {
  execute: () => void
  undo: () => void
  snapshot: () => CommandSnapshot
}

export class UndoManager {
  private stack: Command[] = []
  private index = -1
  private maxDepth = 100
  private listeners: Set<(canUndo: boolean, canRedo: boolean) => void> = new Set()

  execute(command: Command) {
    command.execute()
    if (this.index < this.stack.length - 1) {
      this.stack = this.stack.slice(0, this.index + 1)
    }
    this.stack.push(command)
    if (this.stack.length > this.maxDepth) {
      this.stack.shift()
    } else {
      this.index++
    }
    this.notify()
  }

  undo() {
    if (this.index < 0) return
    this.stack[this.index].undo()
    this.index--
    this.notify()
  }

  redo() {
    if (this.index >= this.stack.length - 1) return
    this.index++
    this.stack[this.index].execute()
    this.notify()
  }

  canUndo() {
    return this.index >= 0
  }

  canRedo() {
    return this.index < this.stack.length - 1
  }

  getSnapshots(): CommandSnapshot[] {
    return this.stack.map((cmd) => cmd.snapshot())
  }

  subscribe(listener: (canUndo: boolean, canRedo: boolean) => void) {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private notify() {
    const canUndo = this.canUndo()
    const canRedo = this.canRedo()
    this.listeners.forEach((fn) => fn(canUndo, canRedo))
  }

  clear() {
    this.stack = []
    this.index = -1
    this.notify()
  }
}
