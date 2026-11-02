# Direct COM only. Started only by an explicit live-connect command.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::InputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$pm=$null
$targetProcessId=0
$sessionModelReadMode='xml'
$profileEnabled=$false
$profileMetrics=@{}
function Start-Timing {
 if($script:profileEnabled){return [Diagnostics.Stopwatch]::GetTimestamp()}
 return 0
}
function Add-Elapsed([string]$name,[long]$started){
 if(-not $script:profileEnabled){return}
 $seconds=([Diagnostics.Stopwatch]::GetTimestamp()-$started)/[double][Diagnostics.Stopwatch]::Frequency
 if(-not $script:profileMetrics.ContainsKey($name)){$script:profileMetrics[$name]=@{seconds=0.0;count=0}}
 $script:profileMetrics[$name].seconds+=$seconds;$script:profileMetrics[$name].count+=1
}
function Find-TargetApplication([int]$requestedId){
 if($null -eq ('PowerMillComRot' -as [type])){
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class PowerMillComRot {
 [DllImport("ole32.dll")] static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable table);
 [DllImport("ole32.dll")] static extern int CreateBindCtx(int reserved, out IBindCtx context);
 public static object Find(int pid) {
  IRunningObjectTable table=null; IBindCtx context=null; IEnumMoniker items=null;
  try {
   Marshal.ThrowExceptionForHR(GetRunningObjectTable(0,out table));
   Marshal.ThrowExceptionForHR(CreateBindCtx(0,out context)); table.EnumRunning(out items);
   IMoniker[] batch=new IMoniker[1]; string expected="!PowerMILL.Application:"+pid;
   while(items.Next(1,batch,IntPtr.Zero)==0) {
    IMoniker item=batch[0];
    try {
     string name; item.GetDisplayName(context,null,out name);
     if(String.Equals(name,expected,StringComparison.OrdinalIgnoreCase)) {
      object application; table.GetObject(item,out application); return application;
     }
    } catch(COMException) { } finally { if(item!=null) Marshal.ReleaseComObject(item); }
   }
   throw new InvalidOperationException("Requested PowerMill PID is not registered in ROT: "+pid);
  } finally {
   if(items!=null) Marshal.ReleaseComObject(items);
   if(context!=null) Marshal.ReleaseComObject(context);
   if(table!=null) Marshal.ReleaseComObject(table);
  }
 }
}
'@
 }
 return [PowerMillComRot]::Find($requestedId)
}
function Get-Application {
 if($null -eq $script:pm){
  if($script:targetProcessId -gt 0){$script:pm=Find-TargetApplication $script:targetProcessId}
  else{$script:pm=[Runtime.InteropServices.Marshal]::GetActiveObject('PowerMill.Application')}
 }
 return $script:pm
}
function Get-Scalar([string]$expression){
 $raw=(Get-Application).GetParameterXML($expression)
 if([string]::IsNullOrEmpty($raw)){throw "Empty XML: $expression"}
 return ([xml]$raw).DocumentElement.InnerText
}
function Get-ModelIds($app){
 # Read the complete current model collection on EVERY guard; never cache IDs.
 if($script:sessionModelReadMode -eq 'com'){
  $models=@();foreach($model in $app.Project.Models){$models+=[string]$model.ID}
  return $models
 }
 $raw=$app.GetParameterXML("extract(folder('model'),'ID')")
 if([string]::IsNullOrEmpty($raw)){throw 'Empty model identity XML'}
 $document=[xml]$raw
 if($document.DocumentElement.Name -cne 'array'){throw 'Invalid model identity XML root'}
 $seen=New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
 foreach($node in $document.DocumentElement.ChildNodes){
  if($node.NodeType -ne [Xml.XmlNodeType]::Element -or $node.Name -cne 'string' -or $node.SelectNodes('*').Count -ne 0 -or [string]::IsNullOrWhiteSpace($node.InnerText)){throw 'Invalid model identity XML item'}
  if(-not $seen.Add([string]$node.InnerText)){throw 'Repeated model identity'}
  [string]$node.InnerText
 }
}
function Confirm-ModelIds($session){
 # Independent full COM collection at discovery and every command boundary.
 if($session.busy -or $script:sessionModelReadMode -eq 'com'){return}
 $started=Start-Timing
 try {
  $models=@();foreach($model in (Get-Application).Project.Models){$models+=[string]$model.ID}
  $native=@($session.model_ids|ForEach-Object{$_.ToLowerInvariant()}|Sort-Object)
  $complete=@($models|ForEach-Object{$_.ToLowerInvariant()}|Sort-Object)
  if(($native -join '|') -cne ($complete -join '|')){throw 'Native/COM model identity mismatch'}
 }finally{Add-Elapsed 'session.com_crosscheck' $started}
}
function Assert-ReadExpression([string]$expression){
 if([string]::IsNullOrWhiteSpace($expression) -or $expression -match '[\r\n\x00;]'){throw 'Invalid read-only expression'}
 $stripped=[regex]::Replace($expression,'"[^"]*"|''[^'']*''','')
 if($stripped -match '[''"]|(?<![=!<>])=(?!=)'){throw 'Invalid read-only expression syntax'}
 $allowed=@('entity','entity_exists','folder','folder_exists','pathname','project_pathname','size','extract','toolpath_component_count','segment_point_count','segment_get_point','segment_get_length','point_parameters','segment_has_leadin','segment_has_leadout','abs','min','max','components','model_components_thicknessset','smart_models','smart_levels','smart_colours','limits','explorer_selected_entities','machine','machine_axis','simulation_location','simulation_issues','tool_tip_position','tool_axis_direction','orientation_vector_direction','number_selected','get_folders','active_folder','component_has_point_parameter')
 foreach($call in [regex]::Matches($stripped,'\b([A-Za-z_]\w*)\s*\(')){
  if($allowed -notcontains $call.Groups[1].Value){throw 'Expression function is not approved for read-only use'}
 }
}
function Get-Session {
 $totalStarted=Start-Timing
 try {
 $started=Start-Timing;$app=Get-Application;Add-Elapsed 'session.application' $started
 $started=Start-Timing;$wasBusy=$app.Busy;Add-Elapsed 'session.initial_busy' $started
 if($wasBusy){return [ordered]@{process_id=[int]$app.Debug.ProcessID;version=[int]$app.Version;busy=$true;model_ids=$null;project_path=$null}}
 $started=Start-Timing
 $models=@(Get-ModelIds $app)
 Add-Elapsed 'session.models' $started
 $started=Start-Timing;$currentProcessId=[int]$app.Debug.ProcessID;Add-Elapsed 'session.pid' $started
 $started=Start-Timing;$currentVersion=[int]$app.Version;Add-Elapsed 'session.version' $started
 $started=Start-Timing;$busyNow=[bool]$app.Busy;Add-Elapsed 'session.final_busy' $started
 $started=Start-Timing;$currentProjectPath=Get-Scalar 'project_pathname(0)';Add-Elapsed 'session.project_path' $started
 return [ordered]@{process_id=$currentProcessId;version=$currentVersion;busy=$busyNow;model_ids=@($models);project_path=$currentProjectPath}
 }finally{Add-Elapsed 'session.total' $totalStarted}
}
function Assert-Identity($guard,$session){
 $started=Start-Timing
 try {
 if(@(2023000,2023003,2023101) -notcontains $guard.version){throw 'Only the verified PowerMill 2023 profile is supported'}
 if($session.process_id -ne $guard.process_id -or $session.version -ne $guard.version){throw 'Process/version identity mismatch'}
 if($session.busy){return}
 $expected=@($guard.model_ids|ForEach-Object{$_.ToLowerInvariant()}|Sort-Object)
 $actual=@($session.model_ids|ForEach-Object{$_.ToLowerInvariant()}|Sort-Object)
 if(($actual -join '|') -cne ($expected -join '|')){throw 'Project model identity mismatch'}
 if($null -ne $guard.PSObject.Properties['project_path']){
  if($session.project_path.Replace('/','\').ToLowerInvariant() -cne $guard.project_path.Replace('/','\').ToLowerInvariant()){throw 'Project pathname mismatch'}
 }
 }finally{Add-Elapsed 'guard.identity_compare' $started}
}
function Assert-Idle($guard){
 $started=Start-Timing
 try {
 $session=Get-Session;Assert-Identity $guard $session
 if($session.busy){throw 'PowerMill is busy; no command dispatched'}
 return $session
 }finally{Add-Elapsed 'guard.idle' $started}
}
function Root-Probe($guard){
 $rootStarted=Start-Timing
 try {
 $session=Get-Session;Assert-Identity $guard $session
 if($session.busy){$session['root_ready']=$false;return $session}
 Confirm-ModelIds $session
 $output=$null;$started=Start-Timing
 try{$code=(Get-Application).DoCommandEx('HELP',[ref]$output)}finally{Add-Elapsed 'guard.help' $started}
 $root=($code -eq 0 -and $output -match '(?im)^[ \t]*Commands available[ \t]*:[ \t\r]*$' -and $output -match '(?m)^[ \t]*ACTIVATE(?:[ \t]+[^\r\n]*)?[\r]*$' -and $output -match '(?m)^[ \t]*CREATE(?:[ \t]+[^\r\n]*)?[\r]*$' -and $output -match '(?m)^[ \t]*DEACTIVATE(?:[ \t]+[^\r\n]*)?[\r]*$')
 $session['root_ready']=[bool]$root;$session['context_output']=[string]$output
 return $session
 }finally{Add-Elapsed 'guard.root' $rootStarted}
}
while($null -ne ($line=[Console]::ReadLine())){
 $profileMetrics=@{};$requestStarted=Start-Timing
 $request=$null;$dispatched=$false
 try{
  $request=$line|ConvertFrom-Json
  $payload=$request.payload;$guard=$payload.guard
  switch($request.method){
   'session' {$result=Get-Session;Confirm-ModelIds $result}
   'probe' {$result=Root-Probe $guard}
   'read_xml' {
    foreach($expression in $payload.expressions){Assert-ReadExpression ([string]$expression)}
    $null=Assert-Idle $guard;$values=New-Object 'Collections.Generic.List[string]'
    $first=$true
    foreach($expression in $payload.expressions){
     if(-not $first){$null=Assert-Idle $guard};$first=$false
     $started=Start-Timing
     try{$raw=(Get-Application).GetParameterXML([string]$expression)}finally{
      $kind='other'
      if(([string]$expression).StartsWith("entity('toolpath',")){$kind='toolpath'}
      elseif(([string]$expression).StartsWith("entity('machinetool',")){$kind='machine'}
      elseif(([string]$expression).StartsWith("entity('tool',")){$kind='tool'}
      elseif($expression -eq 'simulationstate' -or $expression -eq 'powermill.Simulation'){$kind='settings'}
      Add-Elapsed ('xml.target.'+$kind) $started
     }
     if([string]::IsNullOrEmpty($raw)){throw "Empty XML: $expression"}
     $values.Add([string]$raw)
    }
    $null=Assert-Idle $guard
    $result=@($values.ToArray())
   }
   'execute' {
    $session=Root-Probe $guard
    if($session.busy -or -not $session.root_ready){throw 'Parser not ready; no command dispatched'}
    $op=$payload.operation;$watch=[Diagnostics.Stopwatch]::StartNew()
    $artifacts=@();if($null -ne $op.artifact){$artifacts+=@($op.artifact)}
    if($null -ne $op.dependencies){$artifacts+=@($op.dependencies)}
    foreach($artifact in $artifacts){
     $bytes=[IO.File]::ReadAllBytes($artifact.path);$sha=[Security.Cryptography.SHA256]::Create()
     try{$hash=[BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
     if($hash -cne $artifact.sha256){throw 'Macro changed after preflight'}
    }
    if($null -ne $op.artifact -and $op.command -cne ('MACRO "'+$op.artifact.path+'"')){throw 'Macro path mismatch'}
    if($op.trust_macro){
     foreach($artifact in $artifacts){
      $output=$null;$dispatched=$true;$code=(Get-Application).DoCommandEx(('TRUST FILE "'+$artifact.path+'"'),[ref]$output)
      if($code -ne 0 -or [string]$output -match '(?im)unknown command|invalid command|unexpected input|(^|\n)\s*(Ошибка|Error)\s*:'){throw ('Specific macro trust failed: '+$output)}
     }
    }
    $output=$null;$dispatched=$true;$code=(Get-Application).DoCommandEx([string]$op.command,[ref]$output)
    $result=[ordered]@{dispatched=$true;code=[int]$code;output=[string]$output;seconds=$watch.Elapsed.TotalSeconds}
   }
   default {throw 'Unknown worker method'}
  }
  $response=[ordered]@{id=$request.id;ok=$true;result=$result}
 }catch{
  $response=[ordered]@{id=$request.id;ok=$false;dispatched=$dispatched;error=$_.Exception.Message}
 }
 if($profileEnabled){
  $response['profile']=@{request_seconds=([Diagnostics.Stopwatch]::GetTimestamp()-$requestStarted)/[double][Diagnostics.Stopwatch]::Frequency;metrics=$profileMetrics}
  if($response.ok -and $request.method -eq 'execute'){$response.profile['native_envelope_seconds']=$result.seconds}
 }
 [Console]::WriteLine(($response|ConvertTo-Json -Depth 60 -Compress))
}
