// Animation-only overlay on the locked weather5 card HTML. Reads all values from the DOM (source data); final state == static card.
(function(){
const q=new URLSearchParams(location.hash.slice(1)); const V=q.get('v'), t=parseFloat(q.get('t'));
const eo=x=>1-Math.pow(1-Math.min(1,Math.max(0,x)),3);
const p=(s,d)=>eo((t-s)/d);
const back=x=>{x=Math.min(1,Math.max(0,x));const c=1.70158;return 1+(c+1)*Math.pow(x-1,3)+c*Math.pow(x-1,2);};
const k=document.querySelector('.k'),h=document.querySelector('.h'),sub=h.nextElementSibling,br=document.querySelector('.brand');
const rows=[...document.querySelectorAll('.r')].map(r=>{const c=r.children;const circ=c[3].querySelector('circle');
 return {name:c[0],num:c[1].querySelector('span'),qual:c[2],line:c[3].querySelector('line'),rect:c[3].querySelector('rect'),circ,cx:parseFloat(circ.getAttribute('cx')),val:parseFloat(c[1].textContent)};});
const fade=(el,a,dy)=>{el.style.opacity=a;if(dy)el.style.transform=`translateY(${(1-a)*dy}px)`;};
let T; // timing table
if(V==='A'||V==='F'){T={k:[0,.4],h:[.3,.5],sub:[.7,.4],br:[.7,.4],rows:rows.map((_,i)=>({n:[1.0+.2*i,.4],b:[1.8+.2*i,.5],c:[2.6+.35*i,1.0],q:[4.3+.15*i,.35]}))};}
if(V==='B'){T={k:[0,.3],h:[.1,.35],sub:[.2,.3],br:[.2,.3],rows:rows.map((_,i)=>({n:[.3,.35],b:[.45,.35],c:[.8,1.6],q:[2.45,.35]}))};}
if(V==='C'){const L=rows.length-1;T={k:[0,.3],h:[4.5,.5],sub:[.2,.3],br:[.2,.3],rows:rows.map((_,i)=>i<L?{n:[.3,.35],b:[.45,.35],c:[1.0+.12*i,1.4],q:[2.7,.35]}:{n:[.3,.35],b:[.45,.35],c:[3.0,1.4],q:[4.4,.35]})};}
fade(k,p(...T.k),12);fade(h,p(...T.h),16);fade(sub,p(...T.sub));fade(br,p(...T.br));
rows.forEach((r,i)=>{const R=T.rows[i];
 fade(r.name,p(...R.n),0);r.name.style.opacity=p(...R.n);
 const b=p(...R.b);r.line.style.opacity=b;r.line.setAttribute('x2',300*b);r.rect.style.opacity=b;
 const c=p(...R.c),on=t>=R.c[0]?1:0;r.circ.style.opacity=on;r.circ.setAttribute('cx',(r.cx*c).toFixed(1));
 r.num.style.opacity=on;const v0=V==='F'?10:0;r.num.textContent=(c>=1?r.val:Math.round((v0+(r.val-v0)*c)*10)/10).toFixed(1);
 const qa=Math.min(1,Math.max(0,(t-R.q[0])/R.q[1]));r.qual.style.opacity=eo(qa);r.qual.style.transform=`scale(${qa>=1?1:0.85+0.15*back(qa)})`;r.qual.style.transformOrigin='left center';
 if(t>=R.q[0]+R.q[1]){r.qual.style.transform='';}
});
})();
