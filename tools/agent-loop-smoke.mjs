const [url, token, sessionId, mode] = process.argv.slice(2)
if (!url || !token || !sessionId || !mode) throw new Error('url, token, session and mode required')
const platform = 'http://localhost:20200'; const socket = new WebSocket(url)
const steps = [
  { tool: 'file.write', arguments: { path: `loop/${sessionId}.txt`, content: 'loop-control-ok' } },
  { tool: 'code_interpreter', arguments: { code: 'print(sum(i*i for i in range(20000000)))' } },
  { tool: 'file.read', arguments: { path: `loop/${sessionId}.txt` } },
]
let pauseRequested=false, paused=false, resumed=false, cancelled=false, maxBlocked=false, thoughtCount=0, finalTasks=[], versions=[]
const control = async action => fetch(`${platform}/api/v1/sessions/${sessionId}/${action}`, { method:'POST', headers:{ Authorization:`Bearer ${token}`,'Content-Type':'application/json' }, body:JSON.stringify({reason:`loop smoke ${action}`}) })
const timer=setTimeout(()=>{process.stderr.write('Agent loop smoke timed out');process.exit(1)},45000)
socket.addEventListener('open',()=>socket.send(JSON.stringify({type:'message.create',content:`/plan ${JSON.stringify(steps)}`,attachment_ids:[]})))
socket.addEventListener('message',async({data})=>{
  const event=JSON.parse(data); if(typeof event.seq==='number')socket.send(JSON.stringify({type:'event.ack',seq:event.seq}))
  if(event.type==='task.list.updated'){
    versions.push(event.payload.version); finalTasks=event.payload.tasks
    if(!pauseRequested && event.payload.tasks?.[1]?.status==='running' && mode!=='max') { pauseRequested=true; const response=await control('pause'); if(!response.ok) throw new Error(`pause ${response.status}`) }
  }
  if(event.type==='agent.thought')thoughtCount++
  if(event.type==='error'&&event.payload?.code==='E04003')maxBlocked=true
  if(event.type==='session.status'&&event.payload?.to==='paused'){
    paused=true; const action=mode==='cancel'?'cancel':'resume'; const response=await control(action); if(!response.ok)throw new Error(`${action} ${response.status}`)
  }
  if(event.type==='session.status'&&event.payload?.to==='running')resumed=true
  if(event.type==='session.status'&&event.payload?.to==='cancelled')cancelled=true
  const completed=event.type==='session.status'&&event.payload?.reason==='plan_completed'
  const done=mode==='max'?maxBlocked:mode==='cancel'?cancelled:completed
  if(done){clearTimeout(timer);socket.close();const monotonic=versions.every((v,i)=>i===0||v>versions[i-1]);const ok=mode==='max'?maxBlocked:mode==='cancel'?(paused&&cancelled&&finalTasks.some(t=>t.status==='cancelled')):(paused&&resumed&&completed&&thoughtCount===3&&finalTasks.every(t=>t.status==='completed')&&monotonic);process.stdout.write(JSON.stringify({ok,mode,paused,resumed,cancelled,max_blocked:maxBlocked,thought_count:thoughtCount,task_versions_monotonic:monotonic,final_tasks:finalTasks}));process.exit(ok?0:1)}
})
