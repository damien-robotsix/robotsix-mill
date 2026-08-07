The disk gate now checks the container root alongside `data_dir`, via the new
`disk_check_extra_paths` setting. Sandbox containers write package installs to
the Docker overlay, which lives on a different device from the workspace
volume — so a rebase could fail three times with ENOSPC on every command while
the gate, looking only at the data volume, saw 146 GB free and waved the ticket
straight back in. Park notes now name the filesystem that is actually full.
