#import <AVFAudio/AVFAudio.h>
#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

NS_ASSUME_NONNULL_BEGIN

/// These wrap APIs that throw `NSException`. The exception cannot cross a
/// Swift frame, so the `@try` must sit in Objective-C around the call.

BOOL EVAudioAttachAndPrepare(AVAudioEngine *engine, AVAudioFormat * _Nullable * _Nullable outFormat, NSError * _Nullable * _Nullable outError);
BOOL EVAudioInstallTap(AVAudioInputNode *node, AVAudioFrameCount bufferSize, AVAudioFormat *format, AVAudioNodeTapBlock block, NSError * _Nullable * _Nullable outError);
BOOL EVAudioRemoveTap(AVAudioInputNode *node, NSError * _Nullable * _Nullable outError);
BOOL EVAudioStartEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError);
BOOL EVAudioStopEngine(AVAudioEngine *engine, NSError * _Nullable * _Nullable outError);

UNUserNotificationCenter * _Nullable EVNotificationCenterOrNil(void);

BOOL EVRaiseAndCatchForTests(NSError * _Nullable * _Nullable outError);

NS_ASSUME_NONNULL_END
