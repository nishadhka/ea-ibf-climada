program google_speeds

implicit none

integer imax,jmax,ntotal,npoints
integer ix,iy,it,it1,it2,idum
integer ibx1,ibx2,iby1,iby2
integer urban(100,100),seabar(100,100),ilon(1000),ilat(1000)

real alon,alat,tlon,tlat,qalon(1000),qalat(1000)
real airlon,airlat,trainlon,trainlat

real swlon,swlat,gridx,gridy

imax = 60
jmax = 60
swlon = 78.15
swlat = 17.15
gridx = 0.01
gridy = 0.01

airlon = 78.4324
airlat = 17.2410

trainlon = 78.5014
trainlat = 17.4388

! reading urban points

open(unit=11,file='urban_points.csv')
it = 1
read(11,*)
31 read(11,*,end=41) ix,iy,urban(ix,iy),seabar(ix,iy)
  if(urban(ix,iy).eq.1.and.seabar(ix,iy).eq.0) then
    ilon(it) = ix
    ilat(it) = iy
    qalon(it) = swlon + ix*gridx - gridx/2
    qalat(it) = swlat + iy*gridy - gridy/2
    it = it + 1
  endif  
go to 31
41 continue
npoints = it-1
print *, "total urban points read", npoints

ntotal = 0

! matrix grid starting only for the rural grids

do ix = 6,imax,12
do iy = 6,jmax,12
if(urban(ix,iy).eq.0) then
   alon = swlon + ix*gridx - gridx/2
   alat = swlat + iy*gridy - gridy/2

   ibx1 = max(1,ix-6)
   ibx2 = min(ix+6,imax)
   iby1 = max(1,iy-6)
   iby2 = min(iy+6,jmax)

   do it1 = ibx1,ibx2
   do it2 = iby1,iby2

   if((it1.ne.ix.or.it2.ne.iy).and.seabar(it1,it2).eq.0) then
   if(abs(it1-ix).ge.4.or.abs(it2-iy).ge.4) then        
     tlon = swlon + it1*gridx - gridx/2
     tlat = swlat + it2*gridy - gridy/2
     ntotal = ntotal + 1
     write(71,901) ntotal,alon,alat,tlon,tlat
   endif
   endif

   enddo
   enddo

endif
enddo
enddo
print *, "total OD - rural matrix", ntotal

! airport to all urban points

do it = 1,npoints
!   ntotal = ntotal + 1
!   write(71,901) ntotal,airlon,airlat,qalon(it),qalat(it)
enddo
print *, "total OD - after airport to urban points", ntotal

! urban point to point

do ix = 4,imax,8
do iy = 4,jmax,8
   alon = swlon + ix*gridx - gridx/2
   alat = swlat + iy*gridy - gridy/2

   ibx1 = max(1,ix-4)
   ibx2 = min(ix+4,imax)
   iby1 = max(1,iy-4)
   iby2 = min(iy+4,jmax)

   do it1 = ibx1,ibx2
   do it2 = iby1,iby2

   if(it1.ne.ix.or.it2.ne.iy) then
   if(urban(it1,it2).eq.1.and.seabar(it1,it2).eq.0) then
     tlon = swlon + it1*gridx - gridx/2
     tlat = swlat + it2*gridy - gridy/2
!     ntotal = ntotal + 1
!     write(71,901) ntotal,alon,alat,tlon,tlat
   endif
   endif

   enddo
   enddo
   
enddo
enddo
print *, "total OD - after urban point to point", ntotal

! train location to outer ring

do ix = 1,imax
do iy = 1,jmax
   if(((ix.eq.1.or.ix.eq.imax).and.(mod(iy,2).eq.0)).or.((iy.eq.1.or.iy.eq.jmax).and.(mod(ix,2).eq.0))) then
   alon = swlon + ix*gridx - gridx/2
   alat = swlat + iy*gridy - gridy/2
!   ntotal = ntotal + 1
!   write(71,901) ntotal,trainlon,trainlat,alon,alat
   endif
enddo
enddo
print *, "total OD - after train stn to outer grids", ntotal

! airport location to outer ring

do ix = 1,imax
do iy = 1,jmax
   if(((ix.eq.1.or.ix.eq.imax).and.(mod(iy,4).eq.0)).or.((iy.eq.1.or.iy.eq.jmax).and.(mod(ix,4).eq.0))) then
   alon = swlon + ix*gridx - gridx/2
   alat = swlat + iy*gridy - gridy/2
   ntotal = ntotal + 1
   write(71,901) ntotal,airlon,airlat,alon,alat
   endif
enddo
enddo
print *, "total OD - after airport to outer grids", ntotal

! within the urban grids - cris-crossing 3x3 grids

do ix = 1,imax
do iy = 1,jmax
if(urban(ix,iy).eq.1.and.seabar(ix,iy).eq.0) then
   alon = swlon + max(0,(ix-2))*gridx
   alat = swlat + max(0,(iy-2))*gridy
   tlon = swlon + min(ix+1,imax)*gridx
   tlat = swlat + min(iy+1,jmax)*gridy
   ntotal = ntotal + 1
   write(71,901) ntotal,alon,alat,tlon,tlat
   ntotal = ntotal + 1
   write(71,901) ntotal,alon,tlat,tlon,alat
endif
enddo
enddo
print *, "total OD - after urban 3x3 cris-cross", ntotal

901 format (i0,4(",",f0.4))

end
