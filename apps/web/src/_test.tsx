import { useState } from 'react'
export default function Test() {
  const [x] = useState(0)
  return <div data-testid="test">{x}</div>
}
