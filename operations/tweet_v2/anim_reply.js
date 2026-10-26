// Animation-only overlay on the locked reply card C-2026-09-05.html. All values read from the DOM; final state == static PNG.
(function(){
const q=new URLSearchParams(location.hash.slice(1)),V=q.get('v'),t=parseFloat(q.get('t'));
const cl=x=>Math.min(1,Math.max(0,x)),eo=x=>1-Math.pow(1-cl(x),3),p=(s,d)=>eo((t-s)/d);
const num=s=>parseFloat(s.replace('\u2212','-'));
const fmt=(v,signed)=>{const r=Math.round(v*10)/10;if(!signed)return r.toFixed(1);if(r===0)return '0.0';return (r<0?'\u2212':'+')+Math.abs(r).toFixed(1);};
const k=document.querySelector('.k'),h=document.querySelector('.h'),hdr=h.nextElementSibling,wrap=hdr.nextElementSibling;
const ticks=wrap.nextElementSibling,note=ticks.nextElementSibling,br=document.querySelector('.brand');
const kids=[...wrap.children].filter(r=>!r.classList.contains('closed')),axis=kids.pop(); // Closed rows are static
const rows=kids.map(r=>{const c=r.children,rect=c[1].querySelector('rect'),tx=c[1].querySelector('text'),sp=c[2].querySelectorAll('span');
 const x=+rect.getAttribute('x'),w=+rect.getAttribute('width'),neg=x<280,lx=+tx.getAttribute('x');
 return {name:c[0],rect,tx,obsEl:sp[0],fcEl:sp[1],obs:num(sp[0].textContent),fc:num(sp[1].textContent),gapTxt:tx.textContent,obsTxt:sp[0].textContent,
  x,w,neg,off:neg?(x-lx):(lx-(x+w))};});
let T;
if(V==='A')T={kh:[0,.4],h:[.25,.5],hdr:[.6,.4],n:i=>[.9+.15*i,.4],ax:[1.5,.5],tk:[1.8,.4],fc:[2.1,.4],bar:i=>[2.6+.45*i,.9],note:[4.6,.4]};
if(V==='B')T={kh:[0,.3],h:[.1,.35],hdr:[.25,.3],n:i=>[.3,.35],ax:[.45,.35],tk:[.6,.3],fc:[.7,.3],bar:i=>[1.0,1.4],note:[2.4,.35]};
const f=(el,a,dy)=>{el.style.opacity=a;if(dy)el.style.transform=a>=1?'':`translateY(${(1-a)*dy}px)`;};
f(k,p(...T.kh),12);f(h,p(...T.h),16);f(hdr,p(...T.hdr));f(br,p(...T.kh));
axis.style.transformOrigin='top';axis.style.transform=t>=T.ax[0]+T.ax[1]?'':`scaleY(${p(...T.ax)})`;
f(ticks,p(...T.tk));f(note,p(...T.note),8);
rows.forEach((r,i)=>{f(r.name,p(...T.n(i)));f(r.fcEl,p(...T.fc));
 const [s,d]=T.bar(i),e=p(s,d),done=t>=s+d,started=t>=s;
 const gap=Math.round((r.obs-r.fc)*10)/10, g=Math.round(gap*e*10)/10, w=r.w*e;
 r.rect.style.opacity=started&&w>0?1:0;r.tx.style.opacity=started?1:0;r.obsEl.style.opacity=started?1:0;
 if(done){r.rect.setAttribute('width',r.w);r.rect.setAttribute('x',r.x);r.tx.textContent=r.gapTxt;r.obsEl.textContent=r.obsTxt;return;}
 r.rect.setAttribute('width',w.toFixed(1));r.rect.setAttribute('x',(r.neg?280-w:280).toFixed(1));
 r.tx.setAttribute('x',(r.neg?280-w-r.off:280+w+r.off).toFixed(1));
 r.tx.textContent=fmt(g,true);r.obsEl.textContent=fmt(r.fc+g,false);
});
})();
