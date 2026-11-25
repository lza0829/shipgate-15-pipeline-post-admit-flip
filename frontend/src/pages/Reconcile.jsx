import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Reconcile() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/对账').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中</div>
  const s = data.发版统计 || {}
  return (
    <div className="panel">
      <h2>发布对账</h2>
      <div className="grid">
        {Object.entries(s).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
        <div className="stat"><div className="n">{data.晋升事件数}</div><div className="l">晋升事件数</div></div>
        <div className="stat stat-frozen"><div className="n">{data.冻结数 ?? 0}</div><div className="l">冻结数</div></div>
        <div className="stat stat-reject"><div className="n">{data.晋升拒绝数 ?? 0}</div><div className="l">晋升拒绝数</div></div>
      </div>
      <h2>冻结清单（入闸后翻红）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>发版单号</th><th>应用</th><th>状态</th><th>流水线</th><th>入闸快照</th><th>入闸后翻红时间</th><th>当前环境</th></tr></thead>
          <tbody>
            {(data.冻结清单 || []).map((r) => (
              <tr key={r.编号} className="row-frozen">
                <td>{r.发版单号}</td><td>{r.应用}</td><td>{r.状态}</td>
                <td><span className={`lamp lamp-${r.流水线}`}>{r.流水线}</span></td>
                <td>{r.入闸流水线快照 || '-'}</td><td>{r.入闸后翻红时间 || '-'}</td><td>{r.当前环境 || '-'}</td>
              </tr>
            ))}
            {(!data.冻结清单 || data.冻结清单.length === 0) && (
              <tr><td colSpan="7" className="muted">无冻结单</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <h2>晋升拒绝清单（拒因可核）</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>发版单号</th><th>目标环境</th><th>时间</th><th>拒因</th></tr></thead>
          <tbody>
            {(data.晋升拒绝清单 || []).map((r) => (
              <tr key={r.编号}><td>{r.发版单号}</td><td>{r.目标环境 || '-'}</td><td>{r.时间}</td><td>{r.拒因}</td></tr>
            ))}
            {(!data.晋升拒绝清单 || data.晋升拒绝清单.length === 0) && (
              <tr><td colSpan="4" className="muted">无晋升拒绝</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <h2>名册与实例</h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th>应用</th><th>环境</th><th>名册版本</th><th>实例版本们</th><th>红灯数</th></tr></thead>
          <tbody>
            {(data.名册与实例 || []).map((r, i) => (
              <tr key={i}><td>{r.应用}</td><td>{r.环境}</td><td>{r.名册版本}</td><td>{r.实例版本们 || '-'}</td><td>{r.红灯数}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
