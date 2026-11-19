/* «Capacitación Distri Neuquén» (/proyecto, 19.0.1.114.0): role picker with
   hash routing (#armado, #reparto…) and the «Ya lo sé hacer» self-checks,
   saved per device in localStorage (every access in try/catch: private
   windows and blocked storage still work, they just do not remember). */
(function(){
  var ROLES=["armado","reparto","deposito","caja","administracion","vendedores"];
  var sinRol=document.getElementById("sin-rol");
  var botones=document.querySelectorAll(".rolbtn");
  function mostrar(scroll){
    var h=(location.hash||"").replace("#","");
    var rol=ROLES.indexOf(h)>=0?h:"";
    ROLES.forEach(function(r){document.getElementById("rol-"+r).hidden=(r!==rol);});
    sinRol.hidden=!!rol;
    botones.forEach(function(b){b.setAttribute("aria-current",b.dataset.rol===rol?"true":"false");});
    if(rol&&scroll){document.getElementById("tu-rol").scrollIntoView({block:"start"});}
  }
  window.addEventListener("hashchange",function(){mostrar(true);});
  mostrar(false);

  function leer(k){try{return JSON.parse(localStorage.getItem(k)||"{}");}catch(e){return {};}}
  function guardar(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
  document.querySelectorAll("[data-check]").forEach(function(box){
    var key="dn-capacitacion-"+box.dataset.check;
    var marcados=leer(key);
    var inputs=box.querySelectorAll("input[type=checkbox]");
    var out=box.querySelector(".progreso");
    function contar(){
      var n=0;inputs.forEach(function(i){if(i.checked)n++;});
      out.textContent=n===inputs.length?"¡Listo! Ya sabés todo lo de tu rol.":n+" de "+inputs.length+" listos";
    }
    inputs.forEach(function(i){
      i.checked=!!marcados[i.id];
      i.addEventListener("change",function(){marcados[i.id]=i.checked;guardar(key,marcados);contar();});
    });
    contar();
  });
})();
