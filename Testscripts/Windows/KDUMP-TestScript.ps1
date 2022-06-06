# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the Apache License.

param([String] $TestParams,
      [object] $AllVmData)

function Is-VmReadyAfterPanic([string] $VMUserName, [string] $VMPassword, [string] $ip, [int] $port) {
    if ($global:sshPrivateKey) {
        $usePrivateKey = $true
        $credential = $global:sshPrivateKey
    }
    else {
        $usePrivateKey = $false
        $credential = $VMPassword
    }
    $pLinkJobTimeoutInSeconds = 90
    $plinkJob = Start-Job -ScriptBlock {
        Set-Location $args[0];
        if ($Using:usePrivateKey) {
            $output = Write-Output "y" | .\Tools\plink.exe -ssh -C -v -i $args[1] -P $Using:port "$Using:VMUserName@$Using:ip" "sudo -S bash -c 'uname -s'" 2> $null
        }
        else {
            $output = Write-Output "y" | .\Tools\plink.exe -ssh -C -v -pw $args[1] -P $Using:port "$Using:VMUserName@$Using:ip" "echo $Using:password | sudo -S bash -c 'uname -s'" 2> $null
        }
        Write-Output $output
    } -ArgumentList $PWD, $credential, $VMUserName

    $plinkJob | Wait-Job -Timeout $pLinkJobTimeoutInSeconds | Out-Null
    if ($plinkJob.State -eq "Running") {
        Remove-Job -Job $plinkJob -Force | Out-Null
    }
    # No matter the Job ended with "Completed" or not, just read and remove-Job
    $plinkJobConsoleOutput = [string](Receive-Job -Job $plinkJob 2>&1)
    Remove-Job -Job $plinkJob -Force | Out-Null

    Write-LogDbg "Is-VmReadyAfterPanic::Output: $plinkJobConsoleOutput"
    if ($plinkJobConsoleOutput -eq "Linux") {
        return $true
    }
    return $false
}

function Main {
    param (
        $VMName,
        $HvServer,
        $Ipv4,
        $VMPort,
        $VMUserName,
        $VMPassword,
        $RootDir,
        $TestParams,
        $TestPlatform
    )

    $nmi = $null
    $useNFS = $null

    if (-not $TestParams) {
        Write-LogErr "No test parameters specified"
        return "Aborted"
    }
    if (-not $RootDir) {
        Write-LogWarn "No rootdir was specified"
    } else {
        Set-Location $RootDir
    }

    # Parse test parameters
    $params = $testParams.Split(";")
    foreach ($p in $params) {
        $fields = $p.Split("=")

        switch ($fields[0].Trim()) {
          "crashkernel"   { $crashKernel    = $fields[1].Trim() }
          "NMI"           { $nmi = $fields[1].Trim() }
          "VM2NAME"       { $vm2Name = $fields[1].Trim() }
          "use_nfs"       { $useNFS = $fields[1].Trim() }
          "vmCpuNumber"   { $vCPU = $fields[1].Trim() }
          "crashedVcpu"   { $crashedVcpu = $fields[1].Trim()}
          default         {}
        }
    }

    if (-not $crashKernel) {
        Write-LogErr "Test parameter crashkernel was not specified"
        return "FAIL"
    }

    if ($TestPlatform -eq "HyperV") {
        $buildNumber = Get-HostBuildNumber $HvServer
        Write-LogInfo "Found the buildNumber $buildNumber"
        #$RHEL7_Above = Get-VMFeatureSupportStatus -Ipv4 $Ipv4 -SSHPort $VMPort `
        #    -Username $VMUserName -Password $VMPassword -SupportKernel "3.10.0-123"
        $RHEL7_Above = $True
        # WS2012 does not support Debug-VM NMI injection, skipping
        if ( ($buildNumber -le "9200") -and ($nmi -eq 1) ) {
            Write-LogErr "WS2012 does not support Debug-VM NMI injection"
            return "FAIL"
        }
        # Confirm the second VM and NFS
        if ($vm2Name -and $useNFS -eq "yes") {
            $checkState = Get-VM -Name $vm2Name -ComputerName $HvServer

            if ($checkState.State -notlike "Running") {
                Start-VM -Name $vm2Name -ComputerName $HvServer
                if (-not $LASTEXITCODE) {
                    Write-LogErr "Unable to start VM ${vm2Name}"
                    return "FAIL"
                }
                Write-LogInfo "Succesfully started dependency VM ${vm2Name}"
            }

            $newIP = Get-IPv4AndWaitForSSHStart -VMName $VMName -HvServer $HvServer `
                -VmPort $VmPort -User $VMUserName -Password $VMPassword -StepTimeout 30
            if ($newIP) {
                $vm2ipv4 = $newIP
            } else {
                Write-LogErr "Failed to boot up NFS Server $vm2Name"
                return "FAIL"
            }

            $retVal = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "echo 'vm2ipv4=${vm2ipv4}' >> ~/constants.sh"
            if ($retVal -eq $false) {
                Write-LogErr "Failed to echo ${vm2ipv4} to constants.sh"
                return "FAIL"
            }

            $retVal = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "chmod u+x KDUMP-NFSConfig.sh && ./KDUMP-NFSConfig.sh"
            if ($retVal -eq $false) {
                Write-LogErr "Failed to configure the NFS server!"
                return "FAIL"
            }
        }
        # Append host build number to constants.sh
        $retVal = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "echo BuildNumber=$BuildNumber >> ./constants.sh"
    }

    # Configure kdump on the VM
    $retVal = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
        -command "export HOME=``pwd``;chmod u+x KDUMP-Config.sh && ./KDUMP-Config.sh" -runAsSudo
    $state = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
        -command "cat state.txt" -runAsSudo
    if (($state -eq "TestAborted") -or ($state -eq "TestFailed")) {
        Write-LogErr "Running KDUMP-Config.sh script failed on VM!"
        return "ABORTED"
    } elseif ($state -eq "TestSkipped") {
        Write-LogWarn "Distro is not supported or kernel config does not allow auto"
        return "SKIPPED"
    }

    $waitToStartTime=900

    # Rebooting the VM in order to apply the kdump settings
    Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
        -command "sync; reboot" -runAsSudo -RunInBackGround | Out-Null
    Write-LogInfo "Rebooting VM $VMName after kdump configuration..."

    # We need extend the sleep time.
    # If sleeping a shorter time after reboot, the network may haven't stopped even if the VM is rebooting.
    # Then Wait-ForVMToStartSSH will return ture. We will get a pseudomorph that the VM has startup after reboot.
    # But actually not. Then KDUMP-Execute may failed for network error.
    Start-Sleep -Seconds 20 # Wait for kvp & ssh services stop

    # Wait for VM boot up and update ip address
    Wait-ForVMToStartSSH -Ipv4addr $Ipv4 -StepTimeout $waitToStartTime | Out-Null

    # Prepare the kdump related
    $retVal = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "export HOME=``pwd``;chmod u+x KDUMP-Execute.sh && ./KDUMP-Execute.sh" -runAsSudo
    Write-LogInfo "Executed KDUMP-Execute.sh in the VM"
    $state = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
        -command "cat state.txt" -runAsSudo
    if (($state -eq "TestAborted") -or ($state -eq "TestFailed")) {
        Write-LogErr "Running KDUMP-Execute.sh script failed on VM!"
        return "ABORTED"
    }

    # Trigger the kernel panic
    Write-LogInfo "Trigger the kernel panic..."
    if ($nmi -eq 1) {
        # Waiting to kdump_execute.sh to finish execution.
        Write-LogInfo "Let KDUMP-Execute.sh complete"
        Start-Sleep -Seconds 100
        Debug-VM -Name $VMName -InjectNonMaskableInterrupt -ComputerName $HvServer -Force
    } else {
        Write-LogInfo "NMI does not support, so it triggers different method"
        if ($vcpu -eq 4){
            Write-LogInfo "Kdump will be triggered on VCPU 3 of 4"
            Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "taskset -c 2 echo c > /proc/sysrq-trigger" -RunInBackGround -runAsSudo | Out-Null
        } elseif ($crashedVcpu) {
            Write-LogInfo "Kdump will be triggered on $crashedVcpu"
            Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "taskset -c $crashedVcpu echo c > /proc/sysrq-trigger" -RunInBackGround -runAsSudo | Out-Null
        } else {
            # If directly use plink to trigger kdump, command fails to exit, so use start-process
            Write-LogInfo "Set /proc/sysrq-trigger"
            Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "sync; echo c > /proc/sysrq-trigger" -RunInBackGround -runAsSudo | Out-Null
        }
    }

    # Give the host a few seconds to record the event
    Write-LogInfo "Waiting 60 seconds to record the event..."
    Start-Sleep -Seconds 300

    if ($TestPlatform -eq "HyperV") {
        if ((-not $RHEL7_Above) -and ($BuildNumber -eq "14393")){
            Start-Sleep -Seconds 120 # Make sure dump completed
            Stop-VM -VMName $VMName -ComputerName $HvServer -TurnOff -Force
            Start-VM -VMName $VMName -ComputerName $HvServer
        }
    }

    # Wait for VM boot up and update ip address
    $waitTimeOut = $waitToStartTime
    while ($waitTimeOut -gt 0) {
        $sts = Is-VmReadyAfterPanic $VMUserName $VMPassword $Ipv4 $VMPort
        Write-LogInfo "Is-VmReadyAfterPanic: returned $sts"
        if ($sts) {
            break
        }
        $waitTimeOut -= 30
        Start-Sleep -Seconds 30
        Write-LogInfo "waitTimeOut: $waitTimeOut"
    }

    # Verifying if the kernel panic process creates a vmcore file of size 10M+
    $retVal = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "export HOME=``pwd``;chmod u+x KDUMP-Results.sh && ./KDUMP-Results.sh $vm2ipv4" -runAsSudo
    $summary = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort `
                -command "cat summary.log" -runAsSudo
    Write-LogDbg "Result of executing KDUMP-Results.sh - $summary."
    if (-not $retVal) {
        Write-LogErr "Results are not as expected. Check logs for details."
        # Stop NFS server VM
        if ($vm2Name) {
            Stop-VM -VMName $vm2Name -ComputerName $HvServer -Force
        }
        return "FAIL"
    }
    $state = Run-LinuxCmd -username $VMUserName -password $VMPassword -ip $Ipv4 -port $VMPort -command "cat state.txt" -runAsSudo
    if (($state -eq "TestAborted") -or ($state -eq "TestFailed")) {
        Write-LogErr "Running KDUMP-Results.sh script failed on VM!"
        return "ABORTED"
    }

    # Stop NFS server VM
    if ($vm2Name) {
        Stop-VM -VMName $vm2Name -ComputerName $HvServer -Force
    }

    return "PASS"
}

Main -VMName $AllVMData.RoleName -HvServer $GlobalConfig.Global.Hyperv.Hosts.ChildNodes[0].ServerName `
         -ipv4 $AllVMData.PublicIP -VMPort $AllVMData.SSHPort `
         -VMUserName $user -VMPassword $password -rootDir $WorkingDirectory `
         -testParams $testParams -TestPlatform $global:TestPlatform
